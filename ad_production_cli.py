#!/usr/bin/env python3
"""Argparse interface for local, reference-only Ad Production Run orchestration."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Type

from pydantic import BaseModel, ValidationError

from ad_production_run import (
    AdProductionRun,
    AdProductionRunError,
    RunDocumentReference,
    RunProductInput,
    attach_run_reference,
    attach_shot_reference,
    initialize_ad_production_run,
    load_ad_production_run,
    save_ad_production_run,
    set_product_input,
)
from ad_production_status import AdProductionStatusError, resolve_ad_production_status
from campaign import AdCampaign
from confirmed_production_actions import (
    ConfirmedProductionActionError,
    RenderEditingPlanOptions,
    create_generate_hero_preview,
    create_generate_video_preview,
    create_render_editing_plan_preview,
    execute_generate_hero,
    execute_generate_video,
    execute_render_editing_plan,
)
from editing_artifact import EditingArtifact
from editing_audio_assets import EditingAudioManifest
from editing_plan import EditingPlan
from final_ad_review import FinalAdReview, FinalAdReviewHistory
from hero_frame_artifact import HeroFrameArtifact
from hero_frame_generation import HeroFrameGenerationOptions
from hero_frame_review import HeroFrameReview, HeroFrameReviewHistory
from product import Product
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from shot_video_intent import ShotVideoIntent
from storyboard import Storyboard
from storyboard_asset_plan import StoryboardAssetPlan
from production_actions import (
    EditingPlanActionInput,
    ProductionActionError,
    execute_build_editing_plan,
    execute_download_assets,
    execute_prepare_reference_assets,
    execute_record_final_review,
    execute_record_hero_review,
    execute_record_video_review,
    execute_research_product,
)
from video_shot_generation import VideoGenerationOptions
from video_shot_artifact import VideoShotArtifact
from video_shot_review import VideoShotReview, VideoShotReviewHistory


IdentityReader = Callable[[BaseModel], str]
ReferenceSpec = Tuple[str, Type[BaseModel], IdentityReader]

HERO_PROVIDER = "google-gemini"
HERO_MODEL = "gemini-3.1-flash-image"
VIDEO_PROVIDER = "google-gemini-api"
VIDEO_MODEL = "veo-3.1-fast-generate-preview"


RUN_REFERENCE_COMMANDS: Dict[str, ReferenceSpec] = {
    "attach-product": ("product", Product, lambda item: item.id),
    "attach-campaign": ("campaign", AdCampaign, lambda item: item.id),
    "attach-storyboard": ("storyboard", Storyboard, lambda item: item.id),
    "attach-original-assets": (
        "original_asset_manifest",
        ProductAssetManifest,
        lambda item: item.product_id,
    ),
    "attach-derived-assets": (
        "derived_asset_manifest",
        ProductDerivedAssetManifest,
        lambda item: item.product_id,
    ),
    "attach-editing-audio": (
        "editing_audio_manifest",
        EditingAudioManifest,
        lambda item: item.product_id,
    ),
    "attach-asset-plan": ("asset_plan", StoryboardAssetPlan, lambda item: item.id),
    "attach-editing-plan": ("editing_plan", EditingPlan, lambda item: item.id),
    "attach-editing-artifact": (
        "editing_artifact",
        EditingArtifact,
        lambda item: item.id,
    ),
    "attach-final-review": (
        "final_review_history",
        FinalAdReviewHistory,
        lambda item: item.review_context.id,
    ),
}


SHOT_REFERENCE_COMMANDS: Dict[str, ReferenceSpec] = {
    "attach-hero-artifact": (
        "hero_frame_artifact",
        HeroFrameArtifact,
        lambda item: item.id,
    ),
    "attach-hero-review": (
        "hero_frame_review_history",
        HeroFrameReviewHistory,
        lambda item: item.review_context.id,
    ),
    "attach-shot-video-intent": (
        "video_intent",
        ShotVideoIntent,
        lambda item: item.id,
    ),
    "attach-video-artifact": (
        "video_artifact",
        VideoShotArtifact,
        lambda item: item.id,
    ),
    "attach-video-review": (
        "video_review_history",
        VideoShotReviewHistory,
        lambda item: item.review_context.id,
    ),
}


def _add_project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project_id", help="caller-supplied lowercase project slug")


def _add_product_input_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--product-url", help="explicit http/https product page URL")
    group.add_argument("--product-name", help="manual product name")


def _add_attempt_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--attempt-id", required=True)


def _add_paid_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)


def _add_cost_preview_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cost-quote-status", choices=("quoted", "unavailable"), required=True
    )
    parser.add_argument("--quoted-cost-usd", type=float)
    parser.add_argument("--owner-cost-ceiling-usd", type=float)
    parser.add_argument("--allow-unquoted-cost", action="store_true")


def _add_confirmation_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm-preview",
        required=True,
        help="exact action-preview:<sha256> token",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ad-production",
        description="Manage one local, reference-only ad production run.",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="local workspace root (default: current directory)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="create one local project run")
    _add_project_argument(init_parser)
    _add_product_input_arguments(init_parser)

    input_parser = commands.add_parser(
        "set-product-input",
        help="replace product URL/name and clear downstream references",
    )
    _add_project_argument(input_parser)
    required_input = input_parser.add_mutually_exclusive_group(required=True)
    required_input.add_argument("--product-url")
    required_input.add_argument("--product-name")

    for command, help_text in (
        ("status", "derive current workflow and per-shot states"),
        ("validate", "validate run references and current domain authority"),
    ):
        command_parser = commands.add_parser(command, help=help_text)
        _add_project_argument(command_parser)

    for command in RUN_REFERENCE_COMMANDS:
        command_parser = commands.add_parser(
            command,
            help="attach an existing local document reference",
        )
        _add_project_argument(command_parser)
        command_parser.add_argument("path", help="workspace-relative JSON document path")

    for command in SHOT_REFERENCE_COMMANDS:
        command_parser = commands.add_parser(
            command,
            help="attach an existing local document reference to one shot",
        )
        _add_project_argument(command_parser)
        command_parser.add_argument("shot_id", help="Storyboard shot ID")
        command_parser.add_argument("path", help="workspace-relative JSON document path")

    research = commands.add_parser(
        "research-product", help="research exactly one explicit Product URL"
    )
    _add_project_argument(research)
    research.add_argument("--product-url", required=True)
    _add_attempt_argument(research)

    download = commands.add_parser(
        "download-assets", help="download only explicitly selected Product images"
    )
    _add_project_argument(download)
    download.add_argument("--image-id", action="append", required=True)
    _add_attempt_argument(download)

    prepare = commands.add_parser(
        "prepare-reference-assets", help="prepare local reference sheets"
    )
    _add_project_argument(prepare)
    _add_attempt_argument(prepare)

    for command, help_text in (
        ("preview-generate-hero", "preview one Hero generation attempt"),
        ("preview-generate-video", "preview one Video generation attempt"),
    ):
        command_parser = commands.add_parser(command, help=help_text)
        _add_project_argument(command_parser)
        command_parser.add_argument("shot_id")
        _add_paid_arguments(command_parser)
        _add_cost_preview_arguments(command_parser)

    for command, help_text in (
        ("generate-hero", "execute one confirmed Hero generation attempt"),
        ("generate-video", "execute one confirmed Video generation attempt"),
    ):
        command_parser = commands.add_parser(command, help=help_text)
        _add_project_argument(command_parser)
        command_parser.add_argument("shot_id")
        _add_paid_arguments(command_parser)
        _add_confirmation_argument(command_parser)
        _add_attempt_argument(command_parser)

    for command, help_text in (
        ("record-hero-review", "append one strict Hero review"),
        ("record-video-review", "append one strict Video review"),
    ):
        command_parser = commands.add_parser(command, help=help_text)
        _add_project_argument(command_parser)
        command_parser.add_argument("shot_id")
        command_parser.add_argument("--review-file", required=True)
        _add_attempt_argument(command_parser)

    editing = commands.add_parser(
        "build-editing-plan", help="build one EditingPlan from strict authored input"
    )
    _add_project_argument(editing)
    editing.add_argument("--input-file", required=True)
    _add_attempt_argument(editing)

    preview_render = commands.add_parser(
        "preview-render", help="preview one exact local EditingPlan render"
    )
    _add_project_argument(preview_render)

    render = commands.add_parser(
        "render-editing-plan", help="execute one confirmed local EditingPlan render"
    )
    _add_project_argument(render)
    _add_confirmation_argument(render)
    _add_attempt_argument(render)

    final_review = commands.add_parser(
        "record-final-review", help="append one strict Final Ad review"
    )
    _add_project_argument(final_review)
    final_review.add_argument("--review-file", required=True)
    _add_attempt_argument(final_review)

    return parser


def _product_input(arguments: argparse.Namespace) -> Optional[RunProductInput]:
    if arguments.product_url is not None:
        return RunProductInput(kind="product_url", value=arguments.product_url)
    if arguments.product_name is not None:
        return RunProductInput(kind="product_name", value=arguments.product_name)
    return None


def _workspace_document_path(workspace: Path, relative_path: str) -> Path:
    if (
        not relative_path
        or "\\" in relative_path
        or "://" in relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("document path must be a workspace-relative POSIX path")
    parts = relative_path.split("/")
    candidate_path = PurePosixPath(relative_path)
    if (
        candidate_path.is_absolute()
        or str(candidate_path) != relative_path
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError("document path must be a normalized workspace-relative path")

    candidate = workspace / relative_path
    if candidate.is_symlink():
        raise ValueError("document path must not be a symbolic link")
    try:
        resolved_workspace = workspace.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_workspace)
    except (OSError, ValueError) as error:
        raise ValueError(
            "document path must resolve to a file inside the workspace"
        ) from error
    if not resolved.is_file():
        raise ValueError("document path must resolve to a local file")
    return resolved


def _load_reference(
    workspace: Path,
    relative_path: str,
    model_type: Type[BaseModel],
    identity_reader: IdentityReader,
) -> Tuple[RunDocumentReference, BaseModel]:
    document_path = _workspace_document_path(workspace, relative_path)
    try:
        document_bytes = document_path.read_bytes()
        document = model_type.model_validate_json(document_bytes)
    except (OSError, UnicodeError, ValidationError) as error:
        raise ValueError(
            f"document is not valid {model_type.__name__} JSON: {relative_path}"
        ) from error
    identity = identity_reader(document)
    return (
        RunDocumentReference(
            id=identity,
            path=relative_path,
            content_sha256=hashlib.sha256(document_bytes).hexdigest(),
        ),
        document,
    )


def _load_action_input(
    workspace: Path,
    relative_path: str,
    model_type: Type[BaseModel],
) -> BaseModel:
    path = _workspace_document_path(workspace, relative_path)
    try:
        return model_type.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError) as error:
        raise ValueError(
            f"action input is not valid {model_type.__name__} JSON: {relative_path}"
        ) from error


def _print_model(model: BaseModel) -> None:
    print(model.model_dump_json(indent=2, ensure_ascii=False, exclude_none=True))


def _save_mutation(
    run: AdProductionRun,
    candidate: AdProductionRun,
    workspace: Path,
) -> AdProductionRun:
    return save_ad_production_run(
        candidate,
        workspace,
        run.project_id,
        expected_revision=run.revision,
    )


def _preview_output(preview, execution_command: str) -> None:
    print(
        json.dumps(
            {
                "preview": preview.model_dump(mode="json", exclude_none=True),
                "execution_command": execution_command,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def _paid_preview_arguments(arguments: argparse.Namespace) -> dict:
    return {
        "cost_quote_status": arguments.cost_quote_status,
        "quoted_cost_usd": arguments.quoted_cost_usd,
        "owner_cost_ceiling_usd": arguments.owner_cost_ceiling_usd,
        "allow_unquoted_cost": arguments.allow_unquoted_cost,
        "created_at": datetime.now(timezone.utc),
    }


def _project_media_root(workspace: Path, project_id: str) -> Path:
    return workspace / "projects" / project_id / "artifacts" / "media"


def _hero_generator(workspace: Path, project_id: str):
    from gemini_hero_frame_generator import GeminiHeroFrameGenerator

    return GeminiHeroFrameGenerator(
        storage_root=_project_media_root(workspace, project_id)
    )


def _video_generator(workspace: Path, project_id: str):
    from veo_video_generator import VeoVideoGenerator

    return VeoVideoGenerator(storage_root=_project_media_root(workspace, project_id))


def _require_provider(arguments: argparse.Namespace, provider: str, model: str) -> None:
    if arguments.provider != provider or arguments.model != model:
        raise ValueError(
            f"unsupported provider/model; expected {provider}/{model}"
        )


def _execute(arguments: argparse.Namespace) -> int:
    workspace = Path(arguments.workspace)
    if arguments.command == "init":
        run = initialize_ad_production_run(
            workspace,
            arguments.project_id,
            product_input=_product_input(arguments),
        )
        _print_model(run)
        return 0

    run = load_ad_production_run(workspace, arguments.project_id)
    if arguments.command == "status":
        _print_model(resolve_ad_production_status(run, workspace))
        return 0
    if arguments.command == "validate":
        status = resolve_ad_production_status(run, workspace)
        print(
            json.dumps(
                {
                    "valid": not status.issues,
                    "status": status.model_dump(mode="json", exclude_none=True),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if not status.issues else 1
    if arguments.command == "set-product-input":
        candidate = set_product_input(run, _product_input(arguments))
        _print_model(_save_mutation(run, candidate, workspace))
        return 0

    if arguments.command == "research-product":
        _print_model(
            execute_research_product(
                workspace,
                arguments.project_id,
                product_url=arguments.product_url,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "download-assets":
        _print_model(
            execute_download_assets(
                workspace,
                arguments.project_id,
                image_ids=arguments.image_id,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "prepare-reference-assets":
        _print_model(
            execute_prepare_reference_assets(
                workspace,
                arguments.project_id,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "preview-generate-hero":
        _require_provider(arguments, HERO_PROVIDER, HERO_MODEL)
        preview = create_generate_hero_preview(
            workspace,
            arguments.project_id,
            shot_id=arguments.shot_id,
            provider=arguments.provider,
            model=arguments.model,
            options=HeroFrameGenerationOptions(),
            **_paid_preview_arguments(arguments),
        )
        _preview_output(
            preview,
            f"ad-production --workspace <workspace> generate-hero {arguments.project_id} "
            f"{arguments.shot_id} --provider {arguments.provider} "
            f"--model {arguments.model} --confirm-preview {preview.id} "
            "--attempt-id <unique-attempt-id>",
        )
        return 0
    if arguments.command == "generate-hero":
        _require_provider(arguments, HERO_PROVIDER, HERO_MODEL)
        generator = _hero_generator(workspace, arguments.project_id)
        _print_model(
            execute_generate_hero(
                workspace,
                arguments.project_id,
                shot_id=arguments.shot_id,
                provider=arguments.provider,
                model=arguments.model,
                options=HeroFrameGenerationOptions(),
                generator=generator,
                confirm_preview=arguments.confirm_preview,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "record-hero-review":
        review = _load_action_input(
            workspace, arguments.review_file, HeroFrameReview
        )
        _print_model(
            execute_record_hero_review(
                workspace,
                arguments.project_id,
                shot_id=arguments.shot_id,
                review=review,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "preview-generate-video":
        _require_provider(arguments, VIDEO_PROVIDER, VIDEO_MODEL)
        preview = create_generate_video_preview(
            workspace,
            arguments.project_id,
            shot_id=arguments.shot_id,
            provider=arguments.provider,
            model=arguments.model,
            options=VideoGenerationOptions(),
            **_paid_preview_arguments(arguments),
        )
        _preview_output(
            preview,
            f"ad-production --workspace <workspace> generate-video {arguments.project_id} "
            f"{arguments.shot_id} --provider {arguments.provider} "
            f"--model {arguments.model} --confirm-preview {preview.id} "
            "--attempt-id <unique-attempt-id>",
        )
        return 0
    if arguments.command == "generate-video":
        _require_provider(arguments, VIDEO_PROVIDER, VIDEO_MODEL)
        generator = _video_generator(workspace, arguments.project_id)
        _print_model(
            execute_generate_video(
                workspace,
                arguments.project_id,
                shot_id=arguments.shot_id,
                provider=arguments.provider,
                model=arguments.model,
                options=VideoGenerationOptions(),
                generator=generator,
                confirm_preview=arguments.confirm_preview,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "record-video-review":
        review = _load_action_input(
            workspace, arguments.review_file, VideoShotReview
        )
        _print_model(
            execute_record_video_review(
                workspace,
                arguments.project_id,
                shot_id=arguments.shot_id,
                review=review,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "build-editing-plan":
        authored_input = _load_action_input(
            workspace, arguments.input_file, EditingPlanActionInput
        )
        _print_model(
            execute_build_editing_plan(
                workspace,
                arguments.project_id,
                authored_input=authored_input,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "preview-render":
        preview = create_render_editing_plan_preview(
            workspace,
            arguments.project_id,
            options=RenderEditingPlanOptions(),
            created_at=datetime.now(timezone.utc),
        )
        _preview_output(
            preview,
            f"ad-production --workspace <workspace> render-editing-plan {arguments.project_id} "
            f"--confirm-preview {preview.id} --attempt-id <unique-attempt-id>",
        )
        return 0
    if arguments.command == "render-editing-plan":
        _print_model(
            execute_render_editing_plan(
                workspace,
                arguments.project_id,
                options=RenderEditingPlanOptions(),
                confirm_preview=arguments.confirm_preview,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0
    if arguments.command == "record-final-review":
        review = _load_action_input(
            workspace, arguments.review_file, FinalAdReview
        )
        _print_model(
            execute_record_final_review(
                workspace,
                arguments.project_id,
                review=review,
                attempt_id=arguments.attempt_id,
            )
        )
        return 0

    if arguments.command in RUN_REFERENCE_COMMANDS:
        kind, model_type, identity_reader = RUN_REFERENCE_COMMANDS[arguments.command]
        reference, document = _load_reference(
            workspace,
            arguments.path,
            model_type,
            identity_reader,
        )
        shot_ids: Optional[Sequence[str]] = None
        if arguments.command == "attach-storyboard":
            shot_ids = tuple(item.id for item in document.shots)
        candidate = attach_run_reference(
            run,
            kind,
            reference,
            shot_ids=shot_ids,
        )
        _print_model(_save_mutation(run, candidate, workspace))
        return 0

    kind, model_type, identity_reader = SHOT_REFERENCE_COMMANDS[arguments.command]
    reference, _ = _load_reference(
        workspace,
        arguments.path,
        model_type,
        identity_reader,
    )
    candidate = attach_shot_reference(
        run,
        arguments.shot_id,
        kind,
        reference,
    )
    _print_model(_save_mutation(run, candidate, workspace))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return _execute(arguments)
    except (
        AdProductionRunError,
        AdProductionStatusError,
        ConfirmedProductionActionError,
        ProductionActionError,
        OSError,
        UnicodeError,
        ValidationError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
