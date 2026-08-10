#!/usr/bin/env python3
"""Explicit paid smoke test for one approved Hero Frame video request."""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from campaign import load_campaign
from hero_frame_artifact import HeroFrameArtifact
from hero_frame_review import HeroFrameReviewHistory
from product import load_product
from storyboard import load_storyboard
from storyboard_asset_plan import load_storyboard_asset_plan
from veo_video_generator import VeoProviderError, VeoVideoGenerator
from video_shot_generation import VideoGenerationError
from video_shot_request import (
    VideoMotionIntent,
    VideoShotRequestError,
    build_video_shot_request,
)


def _load_model(path: str, model_type):
    return model_type.model_validate_json(Path(path).read_text(encoding="utf-8"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Make exactly one PAID Veo 3.1 Fast 9:16 720p video attempt. "
            "There are no automatic retries."
        )
    )
    parser.add_argument("--product", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--storyboard", required=True)
    parser.add_argument("--asset-plan", required=True)
    parser.add_argument("--hero-frame-artifact", required=True)
    parser.add_argument("--hero-frame-review-history", required=True)
    parser.add_argument("--shot-id", required=True)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--subject-motion")
    parser.add_argument("--environment-motion")
    parser.add_argument("--camera-motion")
    parser.add_argument("--motion-constraints")
    parser.add_argument("--motion-claim-id", action="append", default=[])
    parser.add_argument("--storage-root", default="products")
    parser.add_argument(
        "--confirm-paid-generation",
        action="store_true",
        help="Required acknowledgement that this makes one paid API attempt.",
    )
    args = parser.parse_args(argv)

    if not args.confirm_paid_generation:
        print(
            "Refusing to call Veo without --confirm-paid-generation.",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is required.", file=sys.stderr)
        return 2

    print(
        "WARNING: making exactly one paid Veo video-generation attempt; "
        "maximum approved smoke budget is USD 1.00; no retry or fallback is "
        "configured. Native provider audio may be generated and will be "
        "discarded before editing rather than used as final Campaign audio.",
        file=sys.stderr,
    )
    try:
        artifact = _load_model(args.hero_frame_artifact, HeroFrameArtifact)
        review_history = _load_model(
            args.hero_frame_review_history,
            HeroFrameReviewHistory,
        )
        request = build_video_shot_request(
            product=load_product(args.product),
            campaign=load_campaign(args.campaign),
            storyboard=load_storyboard(args.storyboard),
            asset_plan=load_storyboard_asset_plan(args.asset_plan),
            hero_frame_artifact=artifact,
            review_history=review_history,
            shot_id=args.shot_id,
            requested_generation_duration_seconds=args.duration,
            motion=VideoMotionIntent(
                subject_motion=args.subject_motion,
                environment_motion=args.environment_motion,
                camera_motion=args.camera_motion,
                motion_constraints=args.motion_constraints,
            ),
            motion_relevant_claim_ids=tuple(args.motion_claim_id),
        )
        result = VeoVideoGenerator(
            storage_root=Path(args.storage_root)
        ).generate(request)
    except (
        OSError,
        ValueError,
        VideoShotRequestError,
        VideoGenerationError,
        VeoProviderError,
    ) as error:
        print(f"Error before generation result: {error}", file=sys.stderr)
        return 1

    print(result.model_dump_json(indent=2, exclude_none=True))
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
