"""Durable execution previews, one-time claims, and sanitized attempt receipts."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator
from typing_extensions import Annotated

from ad_production_run import (
    PROJECT_ID_MAX_LENGTH,
    PROJECT_ID_PATTERN,
    RunDocumentReference,
)
from product import NonEmptyStr, ProductModel


ACTION_PREVIEW_VALIDITY = timedelta(minutes=30)
ACTION_PREVIEW_PREFIX = "action-preview:"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")

ActionName = Literal[
    "research_product",
    "download_assets",
    "prepare_reference_assets",
    "record_hero_review",
    "record_video_review",
    "build_editing_plan",
    "record_final_review",
    "generate_hero",
    "generate_video",
    "render_editing_plan",
]
ConfirmedActionName = Literal[
    "generate_hero",
    "generate_video",
    "render_editing_plan",
]
CostQuoteStatus = Literal["not_applicable", "quoted", "unavailable"]
AttemptStatus = Literal[
    "succeeded_and_attached",
    "failed",
    "artifact_created_unattached",
]
AttachmentOutcome = Literal["attached", "not_attached", "conflict", "not_applicable"]
SanitizedMessage = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class FrozenActionModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_datetime(value: Union[datetime, str]) -> str:
    if isinstance(value, datetime):
        value = _utc(value, "preview timestamp").isoformat()
    return value.replace("+00:00", "Z")


def _preview_payload(values: dict) -> dict:
    payload = dict(values)
    payload.pop("id", None)
    payload["created_at"] = _canonical_datetime(payload["created_at"])
    payload["expires_at"] = _canonical_datetime(payload["expires_at"])
    return payload


def _preview_id(values: dict) -> str:
    canonical = json.dumps(
        _preview_payload(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ACTION_PREVIEW_PREFIX + hashlib.sha256(canonical).hexdigest()


def _require_project_id(value: str) -> str:
    if len(value) > PROJECT_ID_MAX_LENGTH or not PROJECT_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "project_id must be a lowercase hyphen-separated slug up to 80 characters"
        )
    return value


def _require_sha256(value: str, field_name: str) -> str:
    if not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return value


class ActionPreview(FrozenActionModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    project_id: NonEmptyStr
    run_revision: int = Field(ge=0, strict=True)
    action: ConfirmedActionName
    request_fingerprint: NonEmptyStr
    provider: Optional[NonEmptyStr] = None
    model: Optional[NonEmptyStr] = None
    options_fingerprint: NonEmptyStr
    requested_output_count: int = Field(gt=0, strict=True)
    cost_quote_status: CostQuoteStatus
    quoted_cost_usd: Optional[float] = Field(default=None, ge=0, strict=True)
    owner_cost_ceiling_usd: Optional[float] = Field(default=None, gt=0, strict=True)
    allow_unquoted_cost: bool = False
    created_at: datetime
    expires_at: datetime

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return _require_project_id(value)

    @field_validator("request_fingerprint", "options_fingerprint")
    @classmethod
    def validate_fingerprints(cls, value: str, info) -> str:
        return _require_sha256(value, info.field_name)

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info) -> datetime:
        return _utc(value, info.field_name)

    @model_validator(mode="after")
    def require_exact_confirmation_contract(self) -> "ActionPreview":
        if self.expires_at - self.created_at != ACTION_PREVIEW_VALIDITY:
            raise ValueError("ActionPreview must expire exactly 30 minutes after creation")
        if self.action in ("generate_hero", "generate_video"):
            if self.provider is None or self.model is None:
                raise ValueError("paid generation preview requires provider and model")
            if self.cost_quote_status == "not_applicable":
                raise ValueError("paid generation cost quote cannot be not_applicable")
        elif self.provider is not None or self.model is not None:
            raise ValueError("local render preview does not accept provider or model")
        if self.cost_quote_status == "quoted":
            if self.quoted_cost_usd is None or self.owner_cost_ceiling_usd is None:
                raise ValueError("quoted cost requires quote and owner ceiling")
            if self.quoted_cost_usd > self.owner_cost_ceiling_usd:
                raise ValueError("quoted cost exceeds owner cost ceiling")
            if self.allow_unquoted_cost:
                raise ValueError("quoted cost cannot use unquoted-cost approval")
        elif self.cost_quote_status == "unavailable":
            if self.quoted_cost_usd is not None:
                raise ValueError("unavailable quote cannot contain an invented cost")
            if not self.allow_unquoted_cost or self.owner_cost_ceiling_usd is None:
                raise ValueError(
                    "unavailable quote requires explicit approval and positive owner ceiling"
                )
        else:
            if any(
                value is not None
                for value in (self.quoted_cost_usd, self.owner_cost_ceiling_usd)
            ) or self.allow_unquoted_cost:
                raise ValueError("not_applicable cost quote forbids cost fields")
        if self.id != _preview_id(self.model_dump(mode="json", exclude_none=False)):
            raise ValueError("ActionPreview ID must match canonical preview content")
        return self


class ActionPreviewClaim(FrozenActionModel):
    schema_version: Literal[1] = 1
    preview_id: NonEmptyStr
    project_id: NonEmptyStr
    attempt_id: NonEmptyStr
    claimed_at: datetime

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return _require_project_id(value)

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        if len(value) > 120 or not ATTEMPT_ID_PATTERN.fullmatch(value):
            raise ValueError("attempt_id must be a filesystem-safe identifier")
        return value

    @field_validator("claimed_at")
    @classmethod
    def validate_claimed_at(cls, value: datetime) -> datetime:
        return _utc(value, "claimed_at")


class ActionAttemptClaim(FrozenActionModel):
    schema_version: Literal[1] = 1
    project_id: NonEmptyStr
    action: ActionName
    attempt_id: NonEmptyStr
    claimed_at: datetime

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return _require_project_id(value)

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        if len(value) > 120 or not ATTEMPT_ID_PATTERN.fullmatch(value):
            raise ValueError("attempt_id must be a filesystem-safe identifier")
        return value

    @field_validator("claimed_at")
    @classmethod
    def validate_claimed_at(cls, value: datetime) -> datetime:
        return _utc(value, "claimed_at")


class ActionFailureDiagnostic(FrozenActionModel):
    code: NonEmptyStr
    message: SanitizedMessage


class ActionAttemptReceipt(FrozenActionModel):
    schema_version: Literal[1] = 1
    attempt_id: NonEmptyStr
    project_id: NonEmptyStr
    action: ActionName
    preview_id: Optional[NonEmptyStr] = None
    started_at: datetime
    completed_at: datetime
    status: AttemptStatus
    run_revision_at_start: int = Field(ge=0, strict=True)
    request_fingerprint: NonEmptyStr
    cost_quote_status: CostQuoteStatus = "not_applicable"
    artifact_reference: Optional[RunDocumentReference] = None
    domain_receipt_reference: Optional[RunDocumentReference] = None
    failure: Optional[ActionFailureDiagnostic] = None
    attachment_outcome: AttachmentOutcome

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        if len(value) > 120 or not ATTEMPT_ID_PATTERN.fullmatch(value):
            raise ValueError("attempt_id must be a filesystem-safe identifier")
        return value

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return _require_project_id(value)

    @field_validator("request_fingerprint")
    @classmethod
    def validate_request_fingerprint(cls, value: str) -> str:
        return _require_sha256(value, "request_fingerprint")

    @field_validator("preview_id")
    @classmethod
    def validate_preview_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            _preview_hash(value)
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info) -> datetime:
        return _utc(value, info.field_name)

    @model_validator(mode="after")
    def require_terminal_outcome_consistency(self) -> "ActionAttemptReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("attempt completion cannot precede its start")
        if self.status == "succeeded_and_attached":
            if self.attachment_outcome != "attached" or self.artifact_reference is None:
                raise ValueError("attached success requires artifact reference and attachment")
            if self.failure is not None:
                raise ValueError("successful attempt cannot contain failure")
        elif self.status == "artifact_created_unattached":
            if self.attachment_outcome != "conflict" or self.artifact_reference is None:
                raise ValueError("unattached artifact requires conflict and artifact reference")
            if self.failure is None:
                raise ValueError("attachment conflict requires failure diagnostic")
        else:
            if self.attachment_outcome not in ("not_attached", "not_applicable"):
                raise ValueError("failed attempt cannot report attachment success")
            if self.failure is None:
                raise ValueError("failed attempt requires failure diagnostic")
        return self


class ActionConfirmationError(ValueError):
    """Raised when a preview, claim, or receipt cannot be trusted."""


def create_action_preview(
    *,
    project_id: str,
    run_revision: int,
    action: ConfirmedActionName,
    request_fingerprint: str,
    options_fingerprint: str,
    requested_output_count: int,
    cost_quote_status: CostQuoteStatus,
    created_at: datetime,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    quoted_cost_usd: Optional[float] = None,
    owner_cost_ceiling_usd: Optional[float] = None,
    allow_unquoted_cost: bool = False,
) -> ActionPreview:
    values = {
        "schema_version": 1,
        "project_id": project_id,
        "run_revision": run_revision,
        "action": action,
        "request_fingerprint": request_fingerprint,
        "provider": provider,
        "model": model,
        "options_fingerprint": options_fingerprint,
        "requested_output_count": requested_output_count,
        "cost_quote_status": cost_quote_status,
        "quoted_cost_usd": quoted_cost_usd,
        "owner_cost_ceiling_usd": owner_cost_ceiling_usd,
        "allow_unquoted_cost": allow_unquoted_cost,
        "created_at": created_at,
        "expires_at": created_at + ACTION_PREVIEW_VALIDITY,
    }
    try:
        return ActionPreview(id=_preview_id(values), **values)
    except (TypeError, ValidationError, ValueError) as error:
        raise ActionConfirmationError(f"ActionPreview is invalid: {error}") from error


def _project_directory(workspace_root: Union[str, Path], project_id: str) -> Path:
    try:
        validated_id = _require_project_id(project_id)
        root = Path(workspace_root).resolve(strict=True)
        project = (root / "projects" / validated_id).resolve(strict=True)
        project.relative_to(root)
    except (OSError, ValueError) as error:
        raise ActionConfirmationError("project directory is unavailable or unsafe") from error
    if not project.is_dir():
        raise ActionConfirmationError("project directory is not a directory")
    return project


def _preview_hash(preview_id: str) -> str:
    if not preview_id.startswith(ACTION_PREVIEW_PREFIX):
        raise ActionConfirmationError("exact action-preview token is required")
    digest = preview_id[len(ACTION_PREVIEW_PREFIX):]
    if not SHA256_PATTERN.fullmatch(digest):
        raise ActionConfirmationError("exact action-preview token is required")
    return digest


def action_preview_path(
    workspace_root: Union[str, Path],
    project_id: str,
    preview_id: str,
) -> Path:
    return _project_directory(workspace_root, project_id) / "action-previews" / f"{_preview_hash(preview_id)}.json"


def _prepare_confined_parent(project: Path, path: Path) -> None:
    try:
        relative_parts = path.parent.relative_to(project).parts
    except ValueError as error:
        raise ActionConfirmationError("action storage path escapes project") from error
    current = project
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise ActionConfirmationError(
                "action storage path must not contain a symbolic link"
            )
        try:
            current.mkdir(exist_ok=True)
            current.resolve(strict=True).relative_to(project)
        except (OSError, ValueError) as error:
            raise ActionConfirmationError(
                "action storage directory is unavailable or unsafe"
            ) from error


def _exclusive_json(path: Path, model: ProductModel, project: Path) -> Path:
    _prepare_confined_parent(project, path)
    if path.is_symlink():
        raise ActionConfirmationError("action storage path must not be a symbolic link")
    temporary_path = None
    try:
        data = (
            model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True)
            + "\n"
        ).encode("utf-8")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".action-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary_path, path, follow_symlinks=False)
    except FileExistsError as error:
        raise ActionConfirmationError(f"action document already exists: {path.name}") from error
    except OSError as error:
        raise ActionConfirmationError(f"action document could not be created: {path.name}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return path


def save_action_preview(preview: ActionPreview, workspace_root: Union[str, Path]) -> Path:
    try:
        current = ActionPreview.model_validate(preview.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ActionConfirmationError(f"ActionPreview is invalid: {error}") from error
    project = _project_directory(workspace_root, current.project_id)
    path = project / "action-previews" / f"{_preview_hash(current.id)}.json"
    return _exclusive_json(path, current, project)


def load_action_preview(path: Union[str, Path]) -> ActionPreview:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ActionConfirmationError("ActionPreview path must not be a symbolic link")
    try:
        return ActionPreview.model_validate_json(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        raise ActionConfirmationError("ActionPreview could not be loaded") from error


def claim_action_preview(
    workspace_root: Union[str, Path],
    confirm_preview: str,
    *,
    project_id: str,
    run_revision: int,
    action: ConfirmedActionName,
    request_fingerprint: str,
    provider: Optional[str],
    model: Optional[str],
    options_fingerprint: str,
    requested_output_count: int,
    attempt_id: str,
    now: datetime,
) -> ActionPreviewClaim:
    preview = load_action_preview(
        action_preview_path(workspace_root, project_id, confirm_preview)
    )
    try:
        current_now = _utc(now, "claim time")
    except ValueError as error:
        raise ActionConfirmationError(str(error)) from error
    expected = (
        project_id,
        run_revision,
        action,
        request_fingerprint,
        provider,
        model,
        options_fingerprint,
        requested_output_count,
    )
    actual = (
        preview.project_id,
        preview.run_revision,
        preview.action,
        preview.request_fingerprint,
        preview.provider,
        preview.model,
        preview.options_fingerprint,
        preview.requested_output_count,
    )
    if preview.id != confirm_preview or actual != expected:
        raise ActionConfirmationError("ActionPreview no longer matches exact execution context")
    if current_now >= preview.expires_at:
        raise ActionConfirmationError("ActionPreview has expired")
    try:
        claim = ActionPreviewClaim(
            preview_id=preview.id,
            project_id=project_id,
            attempt_id=attempt_id,
            claimed_at=current_now,
        )
    except ValidationError as error:
        raise ActionConfirmationError(f"ActionPreview claim is invalid: {error}") from error
    project = _project_directory(workspace_root, project_id)
    _exclusive_json(
        project / "action-preview-claims" / f"{_preview_hash(preview.id)}.json",
        claim,
        project,
    )
    return claim


def claim_action_attempt(
    workspace_root: Union[str, Path],
    *,
    project_id: str,
    action: ActionName,
    attempt_id: str,
    claimed_at: datetime,
) -> ActionAttemptClaim:
    try:
        claim = ActionAttemptClaim(
            project_id=project_id,
            action=action,
            attempt_id=attempt_id,
            claimed_at=claimed_at,
        )
    except ValidationError as error:
        raise ActionConfirmationError(f"action attempt claim is invalid: {error}") from error
    project = _project_directory(workspace_root, project_id)
    _exclusive_json(
        project / "attempt-claims" / action / f"{attempt_id}.json",
        claim,
        project,
    )
    return claim


def action_attempt_receipt_path(
    workspace_root: Union[str, Path],
    receipt: ActionAttemptReceipt,
) -> Path:
    try:
        current = ActionAttemptReceipt.model_validate(receipt.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ActionConfirmationError(f"attempt receipt is invalid: {error}") from error
    return _project_directory(workspace_root, current.project_id) / "attempts" / current.action / f"{current.attempt_id}.json"


def save_action_attempt_receipt(
    receipt: ActionAttemptReceipt,
    workspace_root: Union[str, Path],
) -> Path:
    try:
        current = ActionAttemptReceipt.model_validate(receipt.model_dump())
    except (AttributeError, ValidationError) as error:
        raise ActionConfirmationError(f"attempt receipt is invalid: {error}") from error
    project = _project_directory(workspace_root, current.project_id)
    path = project / "attempts" / current.action / f"{current.attempt_id}.json"
    return _exclusive_json(path, current, project)


def load_action_attempt_receipt(path: Union[str, Path]) -> ActionAttemptReceipt:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ActionConfirmationError("attempt receipt path must not be a symbolic link")
    try:
        return ActionAttemptReceipt.model_validate_json(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        raise ActionConfirmationError("attempt receipt could not be loaded") from error
