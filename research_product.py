#!/usr/bin/env python3
"""Research one explicitly supplied product-page URL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from pydantic import ValidationError

from product import save_product
from product_research import ProductResearchError, research_product


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract deterministic product data from one HTML product page."
    )
    parser.add_argument("product_url", help="Explicit http:// or https:// product-page URL")
    parser.add_argument(
        "--output",
        default="researched_product.json",
        help="Output JSON path (default: researched_product.json)",
    )
    args = parser.parse_args(argv)

    try:
        product = research_product(args.product_url)
        output_path = save_product(product, Path(args.output))
    except (ProductResearchError, ValidationError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print("Researched:")
    print(f"  {product.brand} {product.name}")
    print("\nFound:")
    print(f"  {len(product.specs)} specs")
    print(f"  {len(product.features)} features")
    source_label = "source" if len(product.sources) == 1 else "sources"
    print(f"  {len(product.sources)} {source_label}")
    print("\nSaved:")
    print(f"  {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
