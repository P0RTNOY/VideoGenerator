#!/usr/bin/env python3
"""Explicit paid smoke test for one already-prepared Hero Frame context."""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from campaign import load_campaign
from gemini_hero_frame_generator import (
    GeminiHeroFrameError,
    GeminiHeroFrameGenerator,
)
from hero_frame_generation import HeroFrameGenerationOptions
from hero_frame_request import HeroFrameRequestError, build_hero_frame_request
from product import load_product
from product_asset_manifest import load_product_asset_manifest
from product_derived_asset_manifest import load_product_derived_asset_manifest
from storyboard import load_storyboard
from storyboard_asset_plan import load_storyboard_asset_plan


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Make exactly one PAID Gemini 9:16 1K PNG Hero Frame attempt. "
            "There are no automatic retries."
        )
    )
    parser.add_argument("--product", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--storyboard", required=True)
    parser.add_argument("--asset-plan", required=True)
    parser.add_argument("--original-manifest", required=True)
    parser.add_argument("--derived-manifest")
    parser.add_argument("--shot-id", required=True)
    parser.add_argument("--storage-root", default="products")
    parser.add_argument(
        "--confirm-paid-generation",
        action="store_true",
        help="Required acknowledgement that this makes one paid API attempt.",
    )
    args = parser.parse_args(argv)

    if not args.confirm_paid_generation:
        print(
            "Refusing to call Gemini without --confirm-paid-generation.",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is required.", file=sys.stderr)
        return 2

    print(
        "WARNING: making exactly one paid Gemini image-generation attempt; "
        "no retry or fallback is configured.",
        file=sys.stderr,
    )
    try:
        request = build_hero_frame_request(
            product=load_product(args.product),
            campaign=load_campaign(args.campaign),
            storyboard=load_storyboard(args.storyboard),
            asset_plan=load_storyboard_asset_plan(args.asset_plan),
            shot_id=args.shot_id,
            original_manifest=load_product_asset_manifest(
                args.original_manifest
            ),
            derived_manifest=(
                load_product_derived_asset_manifest(args.derived_manifest)
                if args.derived_manifest
                else None
            ),
        )
        result = GeminiHeroFrameGenerator(
            storage_root=Path(args.storage_root)
        ).generate(request, HeroFrameGenerationOptions())
    except (OSError, ValueError, HeroFrameRequestError, GeminiHeroFrameError) as error:
        print(f"Error before generation result: {error}", file=sys.stderr)
        return 1

    print(result.model_dump_json(indent=2, exclude_none=True))
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
