"""Local, reference-only Ad Production Run workspace persistence."""

import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal, Optional, Sequence, Tuple, Union

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from product import NonEmptyStr, ProductModel


PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PROJECT_ID_MAX_LENGTH = 80

RunReferenceKind = Literal[
    "product",
    "campaign",
    "storyboard",
    "original_asset_manifest",
    "derived_asset_manifest",
    "editing_audio_manifest",
    "asset_plan",
    "editing_plan",
    "editing_artifact",
    "final_review_history",
]
ShotReferenceKind = Literal[
    "hero_frame_artifact",
    "hero_frame_review_history",
    "video_intent",
    "video_artifact",
    "video_review_history",
]


class FrozenRunModel(ProductModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class RunProductInput(FrozenRunModel):
    kind: Literal["product_url", "product_name"]
    value: NonEmptyStr

    @model_validator(mode="after")
    def require_supported_url(self) -> "RunProductInput":
        if self.kind == "product_url" and not self.value.casefold().startswith(
            ("http://", "https://")
        ):
            raise ValueError("product_url input must use http or https")
        return self


class RunDocumentReference(FrozenRunModel):
    id: NonEmptyStr
    path: NonEmptyStr
    content_sha256: Optional[NonEmptyStr] = None

    @field_validator("path")
    @classmethod
    def require_workspace_relative_path(cls, value: str) -> str:
        if "\\" in value or "://" in value or "\x00" in value:
            raise ValueError("document path must be a workspace-relative POSIX path")
        raw_parts = value.split("/")
        if any(part in ("", ".", "..") for part in raw_parts):
            raise ValueError("document path must be normalized without traversal")
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or str(candidate) != value:
            raise ValueError("document path must be normalized and relative")
        return value

    @field_validator("content_sha256")
    @classmethod
    def require_content_sha256(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return value


class RunOperationDiagnostic(FrozenRunModel):
    action: NonEmptyStr
    shot_id: Optional[NonEmptyStr] = None
    code: NonEmptyStr
    message: NonEmptyStr
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def require_utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("diagnostic time must be timezone-aware")
        return value.astimezone(timezone.utc)


class RunShotReferences(FrozenRunModel):
    shot_id: NonEmptyStr
    hero_frame_artifact: Optional[RunDocumentReference] = None
    hero_frame_review_history: Optional[RunDocumentReference] = None
    video_intent: Optional[RunDocumentReference] = None
    video_artifact: Optional[RunDocumentReference] = None
    video_review_history: Optional[RunDocumentReference] = None


class AdProductionRun(FrozenRunModel):
    """One local project and one production run; workflow state is derived."""

    schema_version: Literal[1] = 1
    project_id: NonEmptyStr
    revision: int = Field(default=0, ge=0, strict=True)
    product_input: Optional[RunProductInput] = None
    product: Optional[RunDocumentReference] = None
    campaign: Optional[RunDocumentReference] = None
    storyboard: Optional[RunDocumentReference] = None
    original_asset_manifest: Optional[RunDocumentReference] = None
    derived_asset_manifest: Optional[RunDocumentReference] = None
    editing_audio_manifest: Optional[RunDocumentReference] = None
    asset_plan: Optional[RunDocumentReference] = None
    shots: Tuple[RunShotReferences, ...] = Field(default_factory=tuple)
    editing_plan: Optional[RunDocumentReference] = None
    editing_artifact: Optional[RunDocumentReference] = None
    final_review_history: Optional[RunDocumentReference] = None
    latest_operation_diagnostic: Optional[RunOperationDiagnostic] = None

    @field_validator("project_id")
    @classmethod
    def require_project_slug(cls, value: str) -> str:
        if len(value) > PROJECT_ID_MAX_LENGTH or not PROJECT_ID_PATTERN.fullmatch(
            value
        ):
            raise ValueError(
                "project_id must be a lowercase hyphen-separated slug up to 80 characters"
            )
        return value

    @model_validator(mode="after")
    def require_reference_topology(self) -> "AdProductionRun":
        shot_ids = [shot.shot_id for shot in self.shots]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("run shots must have unique IDs")
        if self.storyboard is None and self.shots:
            raise ValueError("run cannot retain shot references without a Storyboard")
        if self.campaign is not None and self.product is None:
            raise ValueError("run Campaign reference requires a Product reference")
        if self.storyboard is not None and self.campaign is None:
            raise ValueError("run Storyboard reference requires a Campaign reference")
        if self.original_asset_manifest is not None and self.product is None:
            raise ValueError("run asset manifest requires a Product reference")
        if (
            self.derived_asset_manifest is not None
            and self.original_asset_manifest is None
        ):
            raise ValueError("derived asset manifest requires an original manifest")
        if self.editing_audio_manifest is not None and self.product is None:
            raise ValueError("editing audio manifest requires a Product reference")
        if self.asset_plan is not None and (
            self.storyboard is None or self.original_asset_manifest is None
        ):
            raise ValueError(
                "run asset plan requires Storyboard and original asset manifest references"
            )
        if self.editing_artifact is not None and self.editing_plan is None:
            raise ValueError("Editing artifact reference requires an EditingPlan")
        if self.final_review_history is not None and self.editing_artifact is None:
            raise ValueError("Final review reference requires an Editing artifact")
        return self


class AdProductionRunError(ValueError):
    """Base error for local production-run failures."""


class AdProductionRunContextError(AdProductionRunError):
    """Raised when a reference update violates run topology."""


class AdProductionRunPersistenceError(AdProductionRunError):
    """Raised when run.json cannot be safely read or written."""


class AdProductionRunRevisionError(AdProductionRunPersistenceError):
    """Raised when optimistic revision protection detects stale state."""


def _validated_run(run: AdProductionRun) -> AdProductionRun:
    try:
        return AdProductionRun.model_validate(run.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise AdProductionRunContextError(
            f"Ad Production Run must be structurally valid: {error}"
        ) from error


def _validated_reference(reference: RunDocumentReference) -> RunDocumentReference:
    try:
        return RunDocumentReference.model_validate(reference.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise AdProductionRunContextError(
            f"run document reference must be structurally valid: {error}"
        ) from error


def _updated(run: AdProductionRun, **changes) -> AdProductionRun:
    values = run.model_dump()
    values.update(changes)
    try:
        return AdProductionRun.model_validate(values)
    except ValidationError as error:
        raise AdProductionRunContextError(
            f"run reference update is invalid: {error}"
        ) from error


def _empty_shots(run: AdProductionRun) -> Tuple[RunShotReferences, ...]:
    return tuple(RunShotReferences(shot_id=shot.shot_id) for shot in run.shots)


def _editing_clears() -> dict:
    return {
        "editing_plan": None,
        "editing_artifact": None,
        "final_review_history": None,
    }


def create_ad_production_run(
    project_id: str,
    *,
    product_input: Optional[RunProductInput] = None,
) -> AdProductionRun:
    """Create an in-memory revision-zero run without filesystem effects."""

    return AdProductionRun(project_id=project_id, product_input=product_input)


def set_product_input(
    run: AdProductionRun,
    product_input: RunProductInput,
) -> AdProductionRun:
    current = _validated_run(run)
    try:
        new_input = RunProductInput.model_validate(product_input.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise AdProductionRunContextError(
            f"Product input must be structurally valid: {error}"
        ) from error
    if current.product_input == new_input:
        return current.model_copy(deep=True)
    return _updated(
        current,
        product_input=new_input,
        product=None,
        campaign=None,
        storyboard=None,
        original_asset_manifest=None,
        derived_asset_manifest=None,
        editing_audio_manifest=None,
        asset_plan=None,
        shots=(),
        **_editing_clears(),
    )


def _validated_shot_ids(value: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if value is None or isinstance(value, (str, bytes)):
        raise AdProductionRunContextError(
            "attaching a Storyboard requires ordered shot IDs"
        )
    shot_ids = tuple(value)
    try:
        normalized = tuple(
            RunShotReferences(shot_id=shot_id).shot_id for shot_id in shot_ids
        )
    except ValidationError as error:
        raise AdProductionRunContextError(f"invalid Storyboard shot ID: {error}") from error
    if len(normalized) != len(set(normalized)):
        raise AdProductionRunContextError("Storyboard shot IDs must be unique")
    return normalized


def attach_run_reference(
    run: AdProductionRun,
    kind: RunReferenceKind,
    reference: RunDocumentReference,
    *,
    shot_ids: Optional[Sequence[str]] = None,
) -> AdProductionRun:
    """Attach one reference and clear only references that depend on it."""

    current = _validated_run(run)
    new_reference = _validated_reference(reference)
    allowed = {
        "product",
        "campaign",
        "storyboard",
        "original_asset_manifest",
        "derived_asset_manifest",
        "editing_audio_manifest",
        "asset_plan",
        "editing_plan",
        "editing_artifact",
        "final_review_history",
    }
    if kind not in allowed:
        raise AdProductionRunContextError(f"unknown run reference kind: {kind}")
    if kind != "storyboard" and shot_ids is not None:
        raise AdProductionRunContextError("shot IDs are accepted only for Storyboard references")

    if kind == "product":
        if current.product == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            product=new_reference,
            campaign=None,
            storyboard=None,
            original_asset_manifest=None,
            derived_asset_manifest=None,
            editing_audio_manifest=None,
            asset_plan=None,
            shots=(),
            **_editing_clears(),
        )
    if kind == "campaign":
        if current.product is None:
            raise AdProductionRunContextError("attach Product before Campaign")
        if current.campaign == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            campaign=new_reference,
            storyboard=None,
            asset_plan=None,
            shots=(),
            **_editing_clears(),
        )
    if kind == "storyboard":
        if current.campaign is None:
            raise AdProductionRunContextError("attach Campaign before Storyboard")
        new_shot_ids = _validated_shot_ids(shot_ids)
        new_shots = tuple(RunShotReferences(shot_id=item) for item in new_shot_ids)
        if current.storyboard == new_reference and current.shots == new_shots:
            return current.model_copy(deep=True)
        return _updated(
            current,
            storyboard=new_reference,
            asset_plan=None,
            shots=new_shots,
            **_editing_clears(),
        )
    if kind == "original_asset_manifest":
        if current.product is None:
            raise AdProductionRunContextError("attach Product before asset manifest")
        if current.original_asset_manifest == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            original_asset_manifest=new_reference,
            derived_asset_manifest=None,
            asset_plan=None,
            shots=_empty_shots(current),
            **_editing_clears(),
        )
    if kind == "derived_asset_manifest":
        if current.original_asset_manifest is None:
            raise AdProductionRunContextError(
                "attach original asset manifest before derived manifest"
            )
        if current.derived_asset_manifest == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            derived_asset_manifest=new_reference,
            asset_plan=None,
            shots=_empty_shots(current),
            **_editing_clears(),
        )
    if kind == "editing_audio_manifest":
        if current.product is None:
            raise AdProductionRunContextError(
                "attach Product before editing audio manifest"
            )
        if current.editing_audio_manifest == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            editing_audio_manifest=new_reference,
            **_editing_clears(),
        )
    if kind == "asset_plan":
        if current.storyboard is None or current.original_asset_manifest is None:
            raise AdProductionRunContextError(
                "attach Storyboard and original asset manifest before asset plan"
            )
        if current.asset_plan == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            asset_plan=new_reference,
            shots=_empty_shots(current),
            **_editing_clears(),
        )
    if kind == "editing_plan":
        if current.asset_plan is None:
            raise AdProductionRunContextError("attach asset plan before EditingPlan")
        if current.editing_plan == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            editing_plan=new_reference,
            editing_artifact=None,
            final_review_history=None,
        )
    if kind == "editing_artifact":
        if current.editing_plan is None:
            raise AdProductionRunContextError("attach EditingPlan before artifact")
        if current.editing_artifact == new_reference:
            return current.model_copy(deep=True)
        return _updated(
            current,
            editing_artifact=new_reference,
            final_review_history=None,
        )
    if current.editing_artifact is None:
        raise AdProductionRunContextError("attach Editing artifact before final review")
    if current.final_review_history == new_reference:
        return current.model_copy(deep=True)
    return _updated(current, final_review_history=new_reference)


def attach_shot_reference(
    run: AdProductionRun,
    shot_id: str,
    kind: ShotReferenceKind,
    reference: RunDocumentReference,
) -> AdProductionRun:
    """Attach one shot reference and invalidate only its dependent lineage."""

    current = _validated_run(run)
    new_reference = _validated_reference(reference)
    allowed = {
        "hero_frame_artifact",
        "hero_frame_review_history",
        "video_intent",
        "video_artifact",
        "video_review_history",
    }
    if kind not in allowed:
        raise AdProductionRunContextError(f"unknown shot reference kind: {kind}")
    indexes = [index for index, item in enumerate(current.shots) if item.shot_id == shot_id]
    if len(indexes) != 1:
        raise AdProductionRunContextError(f"run shot must resolve exactly once: {shot_id}")
    index = indexes[0]
    shot = current.shots[index]
    if getattr(shot, kind) == new_reference:
        return current.model_copy(deep=True)

    changes = shot.model_dump()
    if kind == "hero_frame_artifact":
        changes.update(
            hero_frame_artifact=new_reference,
            hero_frame_review_history=None,
            video_intent=None,
            video_artifact=None,
            video_review_history=None,
        )
    elif kind == "hero_frame_review_history":
        if shot.hero_frame_artifact is None:
            raise AdProductionRunContextError("attach Hero artifact before Hero review")
        changes.update(
            hero_frame_review_history=new_reference,
            video_intent=None,
            video_artifact=None,
            video_review_history=None,
        )
    elif kind == "video_intent":
        if shot.hero_frame_review_history is None:
            raise AdProductionRunContextError("attach Hero review before Video intent")
        changes.update(
            video_intent=new_reference,
            video_artifact=None,
            video_review_history=None,
        )
    elif kind == "video_artifact":
        if shot.video_intent is None:
            raise AdProductionRunContextError("attach Video intent before Video artifact")
        changes.update(video_artifact=new_reference, video_review_history=None)
    else:
        if shot.video_artifact is None:
            raise AdProductionRunContextError("attach Video artifact before Video review")
        changes.update(video_review_history=new_reference)

    updated_shots = list(current.shots)
    updated_shots[index] = RunShotReferences.model_validate(changes)
    return _updated(current, shots=tuple(updated_shots), **_editing_clears())


def record_operation_diagnostic(
    run: AdProductionRun,
    diagnostic: RunOperationDiagnostic,
) -> AdProductionRun:
    current = _validated_run(run)
    try:
        current_diagnostic = RunOperationDiagnostic.model_validate(
            diagnostic.model_dump()
        )
    except (AttributeError, TypeError, ValidationError) as error:
        raise AdProductionRunContextError(
            f"operation diagnostic must be structurally valid: {error}"
        ) from error
    return _updated(current, latest_operation_diagnostic=current_diagnostic)


def ad_production_run_path(
    workspace_root: Union[str, Path],
    project_id: str,
) -> Path:
    try:
        validated_id = AdProductionRun(project_id=project_id).project_id
    except ValidationError as error:
        raise AdProductionRunPersistenceError(f"invalid project ID: {error}") from error
    return Path(workspace_root) / "projects" / validated_id / "run.json"


def _project_directory(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    create: bool,
) -> Path:
    root = Path(workspace_root)
    directory = ad_production_run_path(root, project_id).parent
    try:
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise AdProductionRunPersistenceError(
                f"project directory does not exist: {directory}"
            )
        directory.resolve().relative_to(root.resolve())
    except AdProductionRunPersistenceError:
        raise
    except (OSError, ValueError) as error:
        raise AdProductionRunPersistenceError(
            "project directory resolves outside the workspace"
        ) from error
    return directory


@contextmanager
def ad_production_run_lock(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    exclusive: bool,
) -> Iterator[None]:
    """Hold the project's cooperative POSIX file lock for one operation."""

    directory = _project_directory(workspace_root, project_id, create=False)
    lock_path = directory / ".run.lock"
    if lock_path.is_symlink():
        raise AdProductionRunPersistenceError("run lock path must not be a symbolic link")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise AdProductionRunPersistenceError("run lock could not be opened") from error
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield
    except OSError as error:
        raise AdProductionRunPersistenceError("run lock operation failed") from error
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_run(path: Path) -> AdProductionRun:
    if path.is_symlink():
        raise AdProductionRunPersistenceError("run.json must not be a symbolic link")
    try:
        return AdProductionRun.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        raise AdProductionRunPersistenceError(
            f"run.json could not be loaded: {path}"
        ) from error


def _atomic_write_run(path: Path, run: AdProductionRun) -> None:
    temporary_path = None
    try:
        text = run.model_dump_json(
            indent=2,
            ensure_ascii=False,
            exclude_none=True,
        ) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".run-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(str(temporary_path), str(path))
        temporary_path = None
    except OSError as error:
        raise AdProductionRunPersistenceError(
            f"run.json could not be saved: {path}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def initialize_ad_production_run(
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    product_input: Optional[RunProductInput] = None,
) -> AdProductionRun:
    """Create projects/<project-id>/run.json exactly once at revision zero."""

    directory = _project_directory(workspace_root, project_id, create=True)
    path = directory / "run.json"
    with ad_production_run_lock(workspace_root, project_id, exclusive=True):
        if path.is_symlink() or path.exists():
            raise AdProductionRunPersistenceError(
                f"project run already exists: {path}"
            )
        try:
            run = create_ad_production_run(
                project_id,
                product_input=product_input,
            )
        except ValidationError as error:
            raise AdProductionRunPersistenceError(
                f"project run is invalid: {error}"
            ) from error
        _atomic_write_run(path, run)
        return run.model_copy(deep=True)


def load_ad_production_run(
    workspace_root: Union[str, Path],
    project_id: str,
) -> AdProductionRun:
    path = ad_production_run_path(workspace_root, project_id)
    with ad_production_run_lock(workspace_root, project_id, exclusive=False):
        current = _read_run(path)
    if current.project_id != project_id:
        raise AdProductionRunPersistenceError(
            "run.json project ID does not match its workspace directory"
        )
    return current.model_copy(deep=True)


def save_ad_production_run(
    run: AdProductionRun,
    workspace_root: Union[str, Path],
    project_id: str,
    *,
    expected_revision: int,
) -> AdProductionRun:
    """Atomically persist a mutation if the current revision still matches."""

    if type(expected_revision) is not int or expected_revision < 0:
        raise AdProductionRunRevisionError(
            "expected revision must be a non-negative integer"
        )
    try:
        candidate = AdProductionRun.model_validate(run.model_dump())
    except (AttributeError, TypeError, ValidationError) as error:
        raise AdProductionRunPersistenceError(
            f"run mutation is structurally invalid: {error}"
        ) from error
    if candidate.project_id != project_id:
        raise AdProductionRunPersistenceError(
            "run mutation project ID does not match the expected workspace"
        )
    if candidate.revision != expected_revision:
        raise AdProductionRunRevisionError(
            "run mutation must be based on the expected revision"
        )
    path = ad_production_run_path(workspace_root, project_id)
    with ad_production_run_lock(
        workspace_root,
        project_id,
        exclusive=True,
    ):
        current = _read_run(path)
        if current.revision != expected_revision:
            raise AdProductionRunRevisionError(
                f"run revision changed from {expected_revision} to {current.revision}"
            )
        current_values = current.model_dump(exclude={"revision"})
        candidate_values = candidate.model_dump(exclude={"revision"})
        if current_values == candidate_values:
            return current.model_copy(deep=True)
        stored = AdProductionRun.model_validate(
            {**candidate.model_dump(), "revision": expected_revision + 1}
        )
        _atomic_write_run(path, stored)
        return stored.model_copy(deep=True)
