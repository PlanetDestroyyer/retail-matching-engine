"""
step1_extractor.py — Dynamic feature extractor + normalizer + image hasher.
Reads raw JSONL → writes normalized feature JSONL.

Usage:
    uv run python src/step1_extractor.py --limit 5
"""

from __future__ import annotations

import re
import sys
import argparse
import io
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import httpx
from PIL import Image
import imagehash
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

try:
    from rembg import remove
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False

from utils import get_logger, jsonl_read, jsonl_append, jsonl_index_composite, load_pairs

logger = get_logger("step1_extractor")

RAW_LINK1      = Path("data/raw/link1_raw.jsonl")
RAW_LINK2      = Path("data/raw/link2_raw.jsonl")
FEAT_LINK1     = Path("data/features/features_link1.jsonl")
FEAT_LINK2     = Path("data/features/features_link2.jsonl")


# ── Key normalization ─────────────────────────────────────────────────────────

def normalize_key(k: str) -> str:
    """'Fit Type' → 'fit_type', 'Color/Colour' → 'color'"""
    k = k.lower().strip()
    k = re.sub(r"[^a-z0-9]+", "_", k)
    k = k.strip("_")

    # Blacklist junk, meta, and feedback keys
    blacklist_substrings = [
        "would_you_like", "tell_us_about", "store_name", "select_province", 
        "please_select", "feedback", "typical", "customer_review", "sellers_rank", 
        "best_sellers", "asin", "upc", "brand_size", "number_of_items", 
        "import_designation", "seasons", "customer_reviews", "manufacturer",
        "tell_us", "lower_price", "state", "t_shirts",
        # Feedback and shipping metadata
        "shipping_fee", "import_charges", "total_estimated_cost", "price"
    ]
    if any(term in k for term in blacklist_substrings):
        return ""

    # Size chart rows and exact match blacklisted keys
    blacklist_exact = {
        "x_small", "small", "medium", "large", "x_large", "xx_large", "xx_small",
        "xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "rating"
    }
    if k in blacklist_exact:
        return ""

    if len(k) <= 1:
        return ""
    if re.search(r"\binr\b|\busd\b", k) or re.search(r"^\d", k):
        return ""
    # Drop keys that look like video/media player controls or UI noise
    _JUNK_KEY_TERMS = [
        "current_time", "duration", "loaded", "buffered", "playback",
        "volume", "seek", "mute", "fullscreen", "progress",
        "add_to", "notify", "sold_by", "ships_from", "report",
        "share", "follow", "subscribe", "toggle",
    ]
    if any(term in k for term in _JUNK_KEY_TERMS):
        return ""
    aliases = {
        "colour": "color", "colour_name": "color", "color_name": "color",
        "size_type": "size", "clothing_size": "size", "product_size": "size",
        "item_weight": "weight", "net_weight": "weight", "shipping_weight": "weight",
        "fit_type": "fit", "garment_fit": "fit", "clothing_fit": "fit",
        "fabric_type": "material", "fabric_composition": "material", "fabric_content": "material", 
        "fabric_material_name": "material", "material_type": "material", "fabric_name": "material",
        "fabric_care_instructions": "care_instructions", "fabric_care": "care_instructions", 
        "care_and_cleaning": "care_instructions", "product_care_instructions": "care_instructions",
        "clothing_neck_style": "neck_style", "neckline": "neck_style",
        "sleeve_length_style": "sleeve_length", "garment_sleeve_style": "sleeve_length", 
        "sleeve_style": "sleeve_length", "sleeve_type": "sleeve_length",
        "clothing_size_group": "gender", "special_size": "gender", "size_group": "gender", 
        "clothing_style": "style", "garment_style": "style", "item_style": "style",
        "clothing_occasion": "occasion", "occasion_type": "occasion",
        "number_of_pieces": "pack_count", "package_quantity": "pack_count",
        "count": "pack_count", "quantity": "pack_count", "count_per_pack": "pack_count", 
        "piece_count": "pack_count",
        "pant_leg_length": "length", "inseam_length": "length",
        "pattern_type": "pattern", "design": "pattern",
        "materials": "material", "colors": "color", "sizes": "size", "styles": "style",
    }
    canonical = aliases.get(k, k)
    if canonical in aliases.values():
        return canonical

    # Rule-based fallback to align related keys, avoiding collisions with metadata keys
    if "sentiment" in canonical or "description" in canonical or "class" in canonical or "group" in canonical:
        return canonical

    if "neck" in canonical or "neckline" in canonical or "collar" in canonical:
        return "neck_style"
    if "sleeve" in canonical:
        return "sleeve_length"
    if "care" in canonical or "wash" in canonical or "clean" in canonical:
        return "care_instructions"
    if "color" in canonical or "colour" in canonical:
        return "color"
    if re.search(r"\b(pack|piece|count|quantity)s?\b", canonical):
        return "pack_count"
    if "material" in canonical or "fabric" in canonical or "composition" in canonical:
        if "weight" not in canonical and "percentage" not in canonical and "stretch" not in canonical:
            return "material"
    return canonical


# ── Value normalization ───────────────────────────────────────────────────────

# Apparel size ordinal map
SIZE_ORDINAL = {
    "xxs": 0, "xs": 1, "s": 2, "small": 2,
    "m": 3, "medium": 3, "med": 3,
    "l": 4, "large": 4, "lg": 4,
    "xl": 5, "x-large": 5, "extra large": 5,
    "xxl": 6, "2xl": 6, "2x": 6,
    "xxxl": 7, "3xl": 7, "3x": 7,
    "4xl": 8, "4x": 8,
    "5xl": 9, "5x": 9,
}

# Weight conversions → grams
WEIGHT_TO_G = {
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
    "kg": 1000.0,  "kilogram": 1000.0, "kilograms": 1000.0,
    "g": 1.0,      "gram": 1.0, "grams": 1.0,
    "mg": 0.001,   "milligram": 0.001,
}

# Volume conversions → ml
VOLUME_TO_ML = {
    "fl oz": 29.5735, "fl. oz": 29.5735, "fluid ounce": 29.5735,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0,
    "cup": 236.588, "cups": 236.588,
    "pt": 473.176, "pint": 473.176,
    "qt": 946.353, "quart": 946.353,
    "gal": 3785.41, "gallon": 3785.41,
}

# Length conversions → cm
LENGTH_TO_CM = {
    "in": 2.54, "inch": 2.54, "inches": 2.54, '"': 2.54,
    "ft": 30.48, "foot": 30.48, "feet": 30.48, "'": 30.48,
    "mm": 0.1, "millimeter": 0.1, "millimetre": 0.1,
    "cm": 1.0, "centimeter": 1.0, "centimetre": 1.0,
    "m": 100.0, "meter": 100.0, "metre": 100.0,
    "yd": 91.44, "yard": 91.44, "yards": 91.44,
}

# Color canonical map
COLOR_ALIASES = {
    "navy": "navy blue", "midnight": "navy blue", "dark blue": "navy blue",
    "royal blue": "blue", "cobalt": "blue", "sky blue": "light blue",
    "heather grey": "grey", "charcoal": "grey", "slate": "grey",
    "heather gray": "grey", "ash": "grey", "silver": "grey",
    "off white": "white", "cream": "white", "ivory": "white", "eggshell": "white",
    "maroon": "dark red", "burgundy": "dark red", "wine": "dark red",
    "forest green": "dark green", "olive": "olive green", "sage": "green",
    "khaki": "tan", "camel": "tan", "sand": "tan", "beige": "tan",
    "hot pink": "pink", "fuchsia": "pink", "magenta": "pink",
    "purple": "purple", "violet": "purple", "lavender": "light purple",
    "coral": "orange", "salmon": "pink",
}


def try_parse_number(s: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except (ValueError, TypeError):
        return None


def normalize_weight(val: str) -> tuple[float, str] | None:
    """Returns (value_in_grams, 'g') or None."""
    val = val.lower().strip()
    for unit, factor in sorted(WEIGHT_TO_G.items(), key=lambda x: -len(x[0])):
        if unit in val:
            num = try_parse_number(val.replace(unit, ""))
            if num is not None:
                return round(num * factor, 4), "g"
    return None


def normalize_volume(val: str) -> tuple[float, str] | None:
    """Returns (value_in_ml, 'ml') or None."""
    val = val.lower().strip()
    for unit, factor in sorted(VOLUME_TO_ML.items(), key=lambda x: -len(x[0])):
        if unit in val:
            num = try_parse_number(val.replace(unit, "").replace("fl", ""))
            if num is not None:
                return round(num * factor, 4), "ml"
    return None


def normalize_length(val: str) -> tuple[float, str] | None:
    """Returns (value_in_cm, 'cm') or None."""
    val = val.lower().strip()
    for unit, factor in sorted(LENGTH_TO_CM.items(), key=lambda x: -len(x[0])):
        if unit in val:
            num = try_parse_number(val.replace(unit, ""))
            if num is not None:
                return round(num * factor, 4), "cm"
    return None


def normalize_size(val: str) -> int | str:
    """Returns ordinal int for apparel sizes, else cleaned string."""
    clean = val.lower().strip()
    if clean in SIZE_ORDINAL:
        return SIZE_ORDINAL[clean]
    return clean


def normalize_pack_count(val: str) -> int | str:
    """'3-pack', 'pack of 3', '2pk', 'single' → 3, 1"""
    val_clean = val.lower().strip()
    
    # Check for direct numeric match first
    m = re.search(r"(\d+)", val_clean)
    if m:
        return int(m.group(1))
        
    text_map = {
        "single": 1, "individual": 1, "one": 1, "pack of one": 1, "1pack": 1, "1pk": 1,
        "double": 2, "pair": 2, "two": 2, "pack of two": 2, "2pack": 2, "2pk": 2,
        "triple": 3, "trio": 3, "three": 3, "pack of three": 3, "3pack": 3, "3pk": 3,
        "four": 4, "quad": 4, "pack of four": 4, "4pack": 4, "4pk": 4,
        "five": 5, "pack of five": 5, "5pack": 5, "5pk": 5,
        "six": 6, "pack of six": 6, "6pack": 6, "6pk": 6,
    }
    
    return text_map.get(val_clean, val_clean)


def normalize_color(val: str) -> str:
    """'Navy Blue' → 'navy blue', with alias resolution."""
    clean = val.lower().strip()
    return COLOR_ALIASES.get(clean, clean)


def normalize_value(key: str, raw_val: str) -> Any:
    """
    Normalize a feature value based on its key context.
    Returns normalized value + optionally a unit-suffixed key.
    """
    v = raw_val.strip()

    # Strip retail noise appended to values from variant selectors and stock badges
    v = re.sub(r"\s*[-–]?\s*(out of stock|in stock|unavailable|sold out)\b.*", "", v, flags=re.I).strip()
    v = re.sub(r"\s+was\s+[\$₹£€]\S+.*$", "", v, flags=re.I).strip()
    v = re.sub(r"\s*\(?\s*\d+\s*(items?|left|remaining)\s*\)?$", "", v, flags=re.I).strip()
    if not v:
        return raw_val.strip()

    if "color" in key or "colour" in key or "shade" in key or "tone" in key:
        return normalize_color(v)

    if key in ("size", "clothing_size", "product_size"):
        return normalize_size(v)

    if re.search(r"\b(pack|piece|count|quantity)s?\b", key):
        return normalize_pack_count(v)

    if "weight" in key:
        result = normalize_weight(v)
        if result: return result[0]  # float in grams

    if "volume" in key or "capacity" in key:
        result = normalize_volume(v)
        if result: return result[0]  # float in ml

    if any(x in key for x in ("length", "width", "height", "dimension")):
        result = normalize_length(v)
        if result: return result[0]  # float in cm

    # Percentage
    if v.endswith("%"):
        try: return float(v[:-1])
        except ValueError: pass

    # Boolean
    if v.lower() in ("yes", "true", "included", "available"):
        return True
    if v.lower() in ("no", "false", "not included", "not available"):
        return False

    # Default: lowercase string
    return v.lower().strip()


# ── Bullet point feature parser ───────────────────────────────────────────────

def parse_bullets(bullets: list[str]) -> dict[str, str]:
    """Extract key: value pairs from bullet points."""
    features = {}
    for bullet in bullets:
        for sep in [":", "—", "-", "|"]:
            if sep in bullet:
                parts = bullet.split(sep, 1)
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip()
                    if k and v and len(k) < 60 and len(v) < 200:
                        nk = normalize_key(k)
                        if nk and nk not in features:
                            features[nk] = v
                        break
    return features


# ── Image hashing ─────────────────────────────────────────────────────────────

def compute_image_hashes(image_urls: list[str]) -> list[dict]:
    """Download images and compute pHash + dHash. Returns list of hash dicts."""
    hashes = []
    for url in image_urls[:3]:  # max 3 images
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                raw_bytes = resp.content
                
                if REMBG_AVAILABLE:
                    try:
                        # Remove background using AI (U-2-Net)
                        processed_bytes = remove(raw_bytes)
                        img_transparent = Image.open(io.BytesIO(processed_bytes)).convert("RGBA")
                        # Composite onto a pure white background for consistent hashing
                        img = Image.new("RGB", img_transparent.size, (255, 255, 255))
                        img.paste(img_transparent, mask=img_transparent.split()[3])
                    except Exception as e:
                        logger.warning(f"rembg failed for {url}: {e}. Falling back to original image.")
                        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
                else:
                    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

                phash = str(imagehash.phash(img))
                dhash = str(imagehash.dhash(img))
                hashes.append({"url": url, "phash": phash, "dhash": dhash})
        except Exception as e:
            logger.debug(f"Image hash failed for {url}: {e}")
    return hashes


def clean_price(p: str) -> str:
    if not p:
        return "N/A"
    # Replace non-breaking spaces and clean whitespace
    p = p.replace("\xa0", " ").strip()
    p = re.sub(r"\s+", " ", p)

    # Check for duplication without spaces
    p_nospace = re.sub(r"\s+", "", p)
    n = len(p_nospace)
    if n % 2 == 0:
        half1 = p_nospace[:n//2]
        half2 = p_nospace[n//2:]
        if half1 == half2:
            p = half1

    # Detect currency type
    is_inr = bool(re.search(r"(?i)(inr|rs\.?|₹)", p))

    # Find the numeric value
    num_match = re.search(r"([0-9,]+\.[0-9]+|[0-9,]+)", p)
    if num_match:
        num_str = num_match.group(1).replace(",", "")
        try:
            val = float(num_str)
            if is_inr:
                # Convert INR to USD (approx rate 83.5)
                val_usd = val / 83.5
                return f"${val_usd:.2f}"
            else:
                # Default to USD
                return f"${val:.2f}"
        except ValueError:
            pass

    # Fallback to cleaning currency symbols
    p = re.sub(r"(?i)\b(inr|rs\.?)\s*", "$", p)
    p = re.sub(r"(?i)\busd\s*", "$", p)
    return p.strip()


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_features(record: dict) -> dict:
    """
    Takes a raw scraped record and returns a normalized feature object.
    Features are fully dynamic — driven by what the page contains.
    """
    raw = record.get("raw", {})
    pair_id = record["pair_id"]
    link_key = record["link_key"]

    # 1. Start from spec_table — all keys dynamic
    raw_features: dict[str, str] = {}
    for k, v in raw.get("spec_table", {}).items():
        nk = normalize_key(k)
        if nk and v:
            raw_features[nk] = v.strip()

    # 2. Merge bullet-point parsed features (don't overwrite spec_table)
    bullet_features = parse_bullets(raw.get("bullet_points", []))
    for k, v in bullet_features.items():
        if k not in raw_features:
            raw_features[k] = v

    # 3. Normalize all values
    normalized_features: dict[str, Any] = {}
    for k, v in raw_features.items():
        normalized_features[k] = normalize_value(k, v)

    # 4. Image hashes
    image_hashes = []
    if record.get("status") == "ok":
        image_hashes = compute_image_hashes(raw.get("image_urls", []))

    primary_phash = image_hashes[0]["phash"] if image_hashes else None
    primary_dhash = image_hashes[0]["dhash"] if image_hashes else None

    return {
        "pair_id": pair_id,
        "link_key": link_key,
        "url": record.get("url", ""),
        "title": raw.get("title", "").strip(),
        "brand": (raw.get("brand", "") or normalized_features.get("brand") or normalized_features.get("brand_name") or "").lower().strip(),
        "price": clean_price(raw.get("price_raw", "")),
        "breadcrumb": raw.get("breadcrumb", []),
        "image_urls": raw.get("image_urls", []),
        "image_hashes": image_hashes,
        "primary_phash": primary_phash,
        "primary_dhash": primary_dhash,
        "features_raw": raw_features,
        "features_normalized": normalized_features,
        "feature_count": len(normalized_features),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def run_extractor(pairs: list[dict]):
    done_link1 = jsonl_index_composite(FEAT_LINK1, ["pair_id", "link_key"])
    done_link2 = jsonl_index_composite(FEAT_LINK2, ["pair_id", "link_key"])

    pair_ids = {p["pair_id"] for p in pairs}
    raw1 = [r for r in jsonl_read(RAW_LINK1) if r["pair_id"] in pair_ids]
    raw2 = [r for r in jsonl_read(RAW_LINK2) if r["pair_id"] in pair_ids]

    logger.info(f"Extracting features: {len(raw1)} link_1 + {len(raw2)} link_2 records")

    with Progress(SpinnerColumn(), TextColumn("[bold purple]{task.description}"),
                  BarColumn(), TaskProgressColumn()) as progress:
        task = progress.add_task("Extracting features...", total=len(raw1) + len(raw2))

        for records, feat_path, done_keys in [
            (raw1, FEAT_LINK1, done_link1),
            (raw2, FEAT_LINK2, done_link2),
        ]:
            for record in records:
                composite = (record["pair_id"], record["link_key"])
                if composite in done_keys:
                    progress.advance(task)
                    continue

                if record.get("status") != "ok":
                    logger.warning(f"[{record['pair_id']}][{record['link_key']}] skipping failed scrape")
                    progress.advance(task)
                    continue

                feat = extract_features(record)
                jsonl_append(feat_path, feat)
                logger.info(
                    f"[{feat['pair_id']}][{feat['link_key']}] "
                    f"features={feat['feature_count']} "
                    f"hashes={len(feat['image_hashes'])}"
                )
                progress.advance(task)

    logger.info("Feature extraction complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    pairs = load_pairs("data.csv", limit=args.limit)
    run_extractor(pairs)
