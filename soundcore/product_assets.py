#!/usr/bin/env python3
"""
product_assets.py

Collect product images from:
  1) one or more product-page URLs, and/or
  2) an internet image search (DDGS)

Then:
  - download high-resolution candidates
  - remove duplicates / tiny images
  - rank likely product shots
  - create a clean multi-view reference sheet
  - export primary.jpg, secondary.jpg, back.jpg
  - write manifest.json with source URLs

Example:
  python product_assets.py \
    --query "Soundcore Select 4 Go green yellow" \
    --domain soundcore.com \
    --out soundcore_assets

Or, preferably for commercial work:
  python product_assets.py \
    --url "https://www.example.com/product-page" \
    --out soundcore_assets

Dependencies:
  pip install requests beautifulsoup4 pillow ddgs
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150 Safari/537.36"
)
TIMEOUT = 20
MAX_BYTES = 25 * 1024 * 1024


@dataclass
class Candidate:
    image_url: str
    source_page: str = ""
    title: str = ""
    source_kind: str = ""
    width: int = 0
    height: int = 0
    score: float = 0.0
    local_path: str = ""
    dhash: str = ""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect, rank, deduplicate, and package product reference images."
    )
    p.add_argument("--query", help="Product search query, e.g. 'Soundcore Select 4 Go green yellow'")
    p.add_argument(
        "--url",
        action="append",
        default=[],
        help="Product page URL. May be supplied multiple times.",
    )
    p.add_argument(
        "--domain",
        default="",
        help="Preferred/official domain. Used as a ranking bonus, e.g. soundcore.com",
    )
    p.add_argument("--out", default="product_assets", help="Output directory")
    p.add_argument("--search-results", type=int, default=40)
    p.add_argument("--keep", type=int, default=12)
    p.add_argument("--sheet-count", type=int, default=6)
    p.add_argument("--min-width", type=int, default=500)
    p.add_argument("--min-height", type=int, default=500)
    p.add_argument(
        "--select",
        default="",
        help="Optional manual candidate indices for primary,secondary,back, e.g. 1,4,7",
    )
    return p.parse_args()


def normalize_url(url: str, base: str = "") -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if base:
        return urljoin(base, url)
    return url


def best_srcset(srcset: str, base: str) -> str:
    best_url = ""
    best_size = -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        u = normalize_url(bits[0], base)
        score = 0
        if len(bits) > 1:
            m = re.match(r"(\d+)(w|x)", bits[1])
            if m:
                score = int(m.group(1))
        if score >= best_size:
            best_url, best_size = u, score
    return best_url


def extract_jsonld_images(data, page_url: str, out: list[Candidate]) -> None:
    if isinstance(data, list):
        for x in data:
            extract_jsonld_images(x, page_url, out)
        return

    if not isinstance(data, dict):
        return

    typ = data.get("@type")
    types = typ if isinstance(typ, list) else [typ]
    if "Product" in types:
        image = data.get("image")
        images = image if isinstance(image, list) else [image]
        for item in images:
            if isinstance(item, str):
                out.append(
                    Candidate(
                        image_url=normalize_url(item, page_url),
                        source_page=page_url,
                        title=str(data.get("name", "")),
                        source_kind="jsonld-product",
                    )
                )
            elif isinstance(item, dict):
                u = item.get("url") or item.get("contentUrl")
                if u:
                    out.append(
                        Candidate(
                            image_url=normalize_url(str(u), page_url),
                            source_page=page_url,
                            title=str(data.get("name", "")),
                            source_kind="jsonld-product",
                        )
                    )

    for v in data.values():
        if isinstance(v, (dict, list)):
            extract_jsonld_images(v, page_url, out)


def scrape_product_page(session: requests.Session, page_url: str) -> list[Candidate]:
    r = session.get(page_url, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: list[Candidate] = []

    for selector, attr, kind in [
        ('meta[property="og:image"]', "content", "og-image"),
        ('meta[property="og:image:secure_url"]', "content", "og-image"),
        ('meta[name="twitter:image"]', "content", "twitter-image"),
    ]:
        for tag in soup.select(selector):
            u = normalize_url(tag.get(attr, ""), page_url)
            if u:
                out.append(Candidate(u, page_url, "", kind))

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            extract_jsonld_images(data, page_url, out)
        except Exception:
            pass

    for tag in soup.find_all(["img", "source"]):
        u = ""
        srcset = tag.get("srcset") or tag.get("data-srcset")
        if srcset:
            u = best_srcset(srcset, page_url)

        if not u:
            for attr in ("data-zoom-image", "data-large-image", "data-src", "src"):
                if tag.get(attr):
                    u = normalize_url(tag.get(attr), page_url)
                    break

        if not u:
            continue

        title = " ".join(
            x for x in [tag.get("alt", ""), tag.get("title", "")] if x
        )
        out.append(Candidate(u, page_url, title, "page-image"))

    return dedupe_candidate_urls(out)


def search_web_images(query: str, max_results: int) -> list[Candidate]:
    try:
        from ddgs import DDGS  # current package
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # older package name
        except ImportError as e:
            raise RuntimeError(
                "Image search requires DDGS. Install with: pip install ddgs"
            ) from e

    out: list[Candidate] = []
    with DDGS() as ddgs:
        results = ddgs.images(
            query=query,
            safesearch="moderate",
            max_results=max_results,
        )
        for item in results:
            image_url = item.get("image") or item.get("thumbnail")
            if not image_url:
                continue
            out.append(
                Candidate(
                    image_url=image_url,
                    source_page=item.get("url", ""),
                    title=item.get("title", ""),
                    source_kind="image-search",
                    width=int(item.get("width") or 0),
                    height=int(item.get("height") or 0),
                )
            )
    return dedupe_candidate_urls(out)


def dedupe_candidate_urls(items: Iterable[Candidate]) -> list[Candidate]:
    seen = set()
    out = []
    for c in items:
        u = c.image_url.split("#")[0]
        if not u or u in seen:
            continue
        seen.add(u)
        c.image_url = u
        out.append(c)
    return out


def looks_like_bad_asset(url: str, title: str) -> bool:
    hay = f"{url} {title}".lower()
    bad = [
        "sprite", "icon", "logo.", "favicon", "avatar", "placeholder",
        "banner", "payment", "badge", "rating", "stars", "tracking",
        ".svg", ".gif",
    ]
    return any(x in hay for x in bad)


def image_dhash(img: Image.Image, hash_size: int = 8) -> str:
    g = ImageOps.grayscale(img).resize((hash_size + 1, hash_size))
    px = list(g.getdata())
    bits = []
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            bits.append(px[offset + col] > px[offset + col + 1])
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"{value:0{hash_size * hash_size // 4}x}"


def hamming_hex(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def download_candidate(
    session: requests.Session,
    c: Candidate,
    out_dir: Path,
    min_width: int,
    min_height: int,
    index: int,
) -> Candidate | None:
    if looks_like_bad_asset(c.image_url, c.title):
        return None

    try:
        with session.get(c.image_url, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            length = int(r.headers.get("content-length") or 0)
            if length and length > MAX_BYTES:
                return None
            content = r.raw.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                return None

        img = Image.open(io.BytesIO(content))
        img.load()
        img = ImageOps.exif_transpose(img).convert("RGB")
        w, h = img.size
        if w < min_width or h < min_height:
            return None

        path = out_dir / f"candidate_{index:03d}.jpg"
        img.save(path, "JPEG", quality=94, optimize=True)

        c.width = w
        c.height = h
        c.local_path = str(path)
        c.dhash = image_dhash(img)
        return c
    except Exception:
        return None


def score_candidate(c: Candidate, query: str, preferred_domain: str) -> float:
    q_tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    hay = f"{c.title} {c.image_url} {c.source_page}".lower()

    score = 0.0

    # Resolution matters, but cap its influence.
    area = max(c.width * c.height, 1)
    score += min(math.log2(area) * 2.0, 45.0)

    # Prefer landscape/square-ish product shots over extremely tall banners.
    ratio = c.width / max(c.height, 1)
    if 0.65 <= ratio <= 1.7:
        score += 8
    elif ratio > 3 or ratio < 0.35:
        score -= 10

    # Exact product/model tokens.
    score += sum(2.2 for t in q_tokens if t in hay)

    # Official/product page sources are better than random aggregators.
    if c.source_kind == "jsonld-product":
        score += 18
    elif c.source_kind == "og-image":
        score += 10
    elif c.source_kind == "page-image":
        score += 5

    if preferred_domain:
        domain = preferred_domain.lower().replace("www.", "")
        if domain in c.source_page.lower() or domain in c.image_url.lower():
            score += 25

    # Words that often indicate useful product views.
    for kw, bonus in [
        ("front", 7), ("side", 7), ("back", 7), ("rear", 7),
        ("angle", 5), ("detail", 4), ("gallery", 3), ("product", 3),
    ]:
        if kw in hay:
            score += bonus

    # Penalize lifestyle shots when building geometry references.
    for kw in ["model", "person", "lifestyle", "adventure", "beach", "family"]:
        if kw in hay:
            score -= 2

    return score


def remove_visual_duplicates(items: list[Candidate], threshold: int = 5) -> list[Candidate]:
    kept: list[Candidate] = []
    for c in sorted(items, key=lambda x: x.score, reverse=True):
        duplicate = False
        for k in kept:
            if c.dhash and k.dhash and hamming_hex(c.dhash, k.dhash) <= threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(c)
    return kept


def copy_as_jpeg(src: Path, dst: Path) -> None:
    img = Image.open(src).convert("RGB")
    img.save(dst, "JPEG", quality=95, optimize=True)


def pick_by_keywords(
    items: list[Candidate],
    keywords: list[str],
    used: set[int],
) -> int | None:
    for idx, c in enumerate(items):
        if idx in used:
            continue
        hay = f"{c.title} {c.image_url} {c.source_page}".lower()
        if any(k in hay for k in keywords):
            return idx
    for idx in range(len(items)):
        if idx not in used:
            return idx
    return None


def create_sheet(
    selected: list[Candidate],
    out_path: Path,
    columns: int = 3,
    tile_size: int = 900,
    margin: int = 24,
) -> None:
    if not selected:
        return

    rows = math.ceil(len(selected) / columns)
    sheet = Image.new(
        "RGB",
        (
            columns * tile_size + (columns + 1) * margin,
            rows * tile_size + (rows + 1) * margin,
        ),
        "white",
    )

    for i, c in enumerate(selected):
        img = Image.open(c.local_path).convert("RGB")
        fitted = ImageOps.contain(img, (tile_size, tile_size), Image.Resampling.LANCZOS)

        x0 = margin + (i % columns) * (tile_size + margin)
        y0 = margin + (i // columns) * (tile_size + margin)

        x = x0 + (tile_size - fitted.width) // 2
        y = y0 + (tile_size - fitted.height) // 2
        sheet.paste(fitted, (x, y))

    sheet.save(out_path, "JPEG", quality=94, optimize=True)


def create_labeled_sheet(
    selected: list[Candidate],
    out_path: Path,
    columns: int = 3,
    tile_size: int = 600,
    label_h: int = 60,
    margin: int = 18,
) -> None:
    if not selected:
        return

    rows = math.ceil(len(selected) / columns)
    sheet = Image.new(
        "RGB",
        (
            columns * tile_size + (columns + 1) * margin,
            rows * (tile_size + label_h) + (rows + 1) * margin,
        ),
        "white",
    )
    draw = ImageDraw.Draw(sheet)

    for i, c in enumerate(selected, start=1):
        img = Image.open(c.local_path).convert("RGB")
        fitted = ImageOps.contain(img, (tile_size, tile_size), Image.Resampling.LANCZOS)

        col = (i - 1) % columns
        row = (i - 1) // columns
        x0 = margin + col * (tile_size + margin)
        y0 = margin + row * (tile_size + label_h + margin)

        x = x0 + (tile_size - fitted.width) // 2
        y = y0 + (tile_size - fitted.height) // 2
        sheet.paste(fitted, (x, y))

        label = f"#{i}  {c.width}x{c.height}"
        draw.text((x0 + 10, y0 + tile_size + 14), label, fill="black")

    sheet.save(out_path, "JPEG", quality=92, optimize=True)


def parse_manual_selection(value: str, count: int) -> list[int] | None:
    if not value:
        return None
    try:
        nums = [int(x.strip()) for x in value.split(",")]
    except ValueError as e:
        raise SystemExit("--select must look like: 1,4,7") from e
    if len(nums) != 3:
        raise SystemExit("--select requires exactly 3 indices: primary,secondary,back")
    idxs = [n - 1 for n in nums]
    if any(i < 0 or i >= count for i in idxs):
        raise SystemExit("One or more --select indices are outside the candidate range.")
    return idxs


def main() -> None:
    args = parse_args()

    if not args.query and not args.url:
        raise SystemExit("Provide --query and/or at least one --url.")

    out_dir = Path(args.out)
    cand_dir = out_dir / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    cand_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})

    candidates: list[Candidate] = []

    for u in args.url:
        print(f"[page] {u}")
        try:
            candidates.extend(scrape_product_page(session, u))
        except Exception as e:
            print(f"  warning: could not scrape {u}: {e}", file=sys.stderr)

    if args.query:
        search_query = args.query
        if args.domain:
            search_query += f" site:{args.domain}"
        print(f"[search] {search_query}")
        try:
            candidates.extend(search_web_images(search_query, args.search_results))
        except Exception as e:
            print(f"  warning: image search failed: {e}", file=sys.stderr)

    candidates = dedupe_candidate_urls(candidates)

    print(f"[download] {len(candidates)} candidate URLs")
    downloaded: list[Candidate] = []

    for i, c in enumerate(candidates, start=1):
        got = download_candidate(
            session,
            c,
            cand_dir,
            args.min_width,
            args.min_height,
            i,
        )
        if got:
            got.score = score_candidate(got, args.query or "", args.domain)
            downloaded.append(got)

    downloaded = remove_visual_duplicates(downloaded)
    downloaded.sort(key=lambda c: c.score, reverse=True)
    downloaded = downloaded[: args.keep]

    if len(downloaded) < 3:
        raise SystemExit(
            f"Only {len(downloaded)} usable images found. "
            "Try a product-page --url, a less restrictive --domain, or a larger --search-results value."
        )

    # Rename candidate files in final rank order for easier inspection.
    ranked_dir = out_dir / "ranked"
    if ranked_dir.exists():
        shutil.rmtree(ranked_dir)
    ranked_dir.mkdir()

    for i, c in enumerate(downloaded, start=1):
        new_path = ranked_dir / f"{i:02d}.jpg"
        shutil.copyfile(c.local_path, new_path)
        c.local_path = str(new_path)

    create_labeled_sheet(downloaded, out_dir / "candidate_sheet_labeled.jpg")

    sheet_items = downloaded[: min(args.sheet_count, len(downloaded))]
    create_sheet(sheet_items, out_dir / "product_reference_sheet.jpg")

    manual = parse_manual_selection(args.select, len(downloaded))

    if manual:
        primary_i, secondary_i, back_i = manual
    else:
        used: set[int] = set()

        primary_i = pick_by_keywords(
            downloaded,
            ["three-quarter", "three quarter", "3/4", "angle", "front"],
            used,
        )
        assert primary_i is not None
        used.add(primary_i)

        secondary_i = pick_by_keywords(
            downloaded,
            ["side", "profile", "left", "right", "angle"],
            used,
        )
        assert secondary_i is not None
        used.add(secondary_i)

        back_i = pick_by_keywords(
            downloaded,
            ["back", "rear", "bottom", "underside"],
            used,
        )
        assert back_i is not None

    copy_as_jpeg(Path(downloaded[primary_i].local_path), out_dir / "primary.jpg")
    copy_as_jpeg(Path(downloaded[secondary_i].local_path), out_dir / "secondary.jpg")
    copy_as_jpeg(Path(downloaded[back_i].local_path), out_dir / "back.jpg")

    manifest = {
        "query": args.query,
        "urls": args.url,
        "preferred_domain": args.domain,
        "selected": {
            "primary": primary_i + 1,
            "secondary": secondary_i + 1,
            "back": back_i + 1,
        },
        "files": {
            "primary": "primary.jpg",
            "secondary": "secondary.jpg",
            "back": "back.jpg",
            "reference_sheet": "product_reference_sheet.jpg",
            "labeled_sheet": "candidate_sheet_labeled.jpg",
        },
        "candidates": [asdict(c) for c in downloaded],
    }

    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\nDONE")
    print(f"Output directory: {out_dir.resolve()}")
    print("Generated:")
    print("  primary.jpg")
    print("  secondary.jpg")
    print("  back.jpg")
    print("  product_reference_sheet.jpg")
    print("  candidate_sheet_labeled.jpg")
    print("  manifest.json")
    print("\nInspect candidate_sheet_labeled.jpg.")
    print("If the auto-selection is imperfect, rerun with e.g. --select 2,5,8")


if __name__ == "__main__":
    main()
