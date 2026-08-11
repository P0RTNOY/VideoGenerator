#!/usr/bin/env python3
"""Argparse interface for local, reference-only Ad Production Run orchestration."""

import argparse
import hashlib
import json
import sys
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
from editing_artifact import EditingArtifact
from editing_audio_assets import EditingAudioManifest
from editing_plan import EditingPlan
from final_ad_review import FinalAdReviewHistory
from hero_frame_artifact import HeroFrameArtifact
from hero_frame_review import HeroFrameReviewHistory
from product import Product
from product_asset_manifest import ProductAssetManifest
from product_derived_asset_manifest import ProductDerivedAssetManifest
from shot_video_intent import ShotVideoIntent
from storyboard import Storyboard
from storyboard_asset_plan import StoryboardAssetPlan
from video_shot_artifact import VideoShotArtifact
from video_shot_review import VideoShotReviewHistory


IdentityReader = Callable[[BaseModel], str]
ReferenceSpec = Tuple[str, Type[BaseModel], IdentityReader]


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
        OSError,
        UnicodeError,
        ValidationError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
