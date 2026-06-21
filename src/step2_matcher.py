"""
step2_matcher.py — Feature diff engine.
For each pair: aligns feature keys, diffs normalized values,
scores title similarity and image hash similarity.

Usage:
    uv run python src/step2_matcher.py --limit 5
"""

from __future__ import annotations

import sys
import argparse
import imagehash
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rapidfuzz import fuzz
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from utils import get_logger, jsonl_read, jsonl_append, jsonl_index, load_pairs

logger = get_logger("step2_matcher")

FEAT_LINK1    = Path("data/features/features_link1.jsonl")
FEAT_LINK2    = Path("data/features/features_link2.jsonl")
DIFFS_OUT     = Path("data/output/match_diffs.jsonl")

# Threshold for fuzzy key alignment
KEY_ALIGN_THRESHOLD = 80  # rapidfuzz score 0-100


def align_keys(keys1: list[str], keys2: list[str]) -> list[tuple[str, str]]:
    """
    Fuzzy-align keys from two feature dicts.
    Returns list of (key_from_link1, key_from_link2) pairs.
    """
    aligned = []
    used2 = set()

    for k1 in keys1:
        best_score, best_k2 = 0, None
        for k2 in keys2:
            if k2 in used2:
                continue
            score = fuzz.ratio(k1, k2)
            if score > best_score:
                best_score, best_k2 = score, k2
        if best_k2 and best_score >= KEY_ALIGN_THRESHOLD:
            aligned.append((k1, best_k2))
            used2.add(best_k2)

    return aligned


def values_match(k: str, v1: object, v2: object) -> bool:
    """
    Compare two normalized values using smart domain-specific rules.
    """
    if type(v1) != type(v2):
        # Try numeric comparison if both can be cast to float
        try:
            f1, f2 = float(v1), float(v2)  # type: ignore[arg-type]
            return abs(f1 - f2) / max(abs(f1), abs(f2), 1e-9) <= 0.01
        except (TypeError, ValueError):
            pass

    if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
        return abs(v1 - v2) / max(abs(v1), abs(v2), 1e-9) <= 0.01

    if isinstance(v1, str) and isinstance(v2, str):
        s1 = v1.strip().lower()
        s2 = v2.strip().lower()

        if not s1 or not s2:
            return s1 == s2

        if s1 == s2:
            return True

        # Brand comparison logic (substring / abbreviation check)
        if k == "brand":
            return s1 in s2 or s2 in s1 or fuzz.token_set_ratio(s1, s2) >= 80

        # Material matching (e.g. "100% cotton jersey" = "jersey")
        if k == "material":
            words = ["cotton", "polyester", "spandex", "jersey", "denim", "nylon", "wool", "silk", "terry", "linen", "rayon", "modal", "viscose"]
            for w in words:
                if w in s1 and w in s2:
                    return True
            return s1 in s2 or s2 in s1 or fuzz.token_set_ratio(s1, s2) >= 80

        # Neck style matching (e.g. "crew neck" = "round")
        if k == "neck_style":
            if any(x in s1 for x in ["crew", "round"]) and any(x in s2 for x in ["crew", "round"]):
                return True
            if "v" in s1 and "v" in s2:
                return True
            if "hood" in s1 and "hood" in s2:
                return True
            if any(x in s1 for x in ["polo", "collar"]) and any(x in s2 for x in ["polo", "collar"]):
                return True
            return s1 in s2 or s2 in s1 or fuzz.token_set_ratio(s1, s2) >= 80

        # Sleeve length matching (e.g. "short sleeve" = "basic sleeve")
        if k == "sleeve_length":
            if "short" in s1 and "short" in s2:
                return True
            if "long" in s1 and "long" in s2:
                return True
            if "sleeveless" in s1 and "sleeveless" in s2:
                return True
            if ("basic" in s1 or "short" in s1) and ("basic" in s2 or "short" in s2):
                return True
            return s1 in s2 or s2 in s1 or fuzz.token_set_ratio(s1, s2) >= 80

        # Care instructions matching (e.g. "machine wash" = "machine washable")
        if k == "care_instructions":
            if "machine" in s1 and "machine" in s2:
                return True
            if "hand" in s1 and "hand" in s2:
                return True
            if "dry clean" in s1 and "dry clean" in s2:
                return True
            return s1 in s2 or s2 in s1 or fuzz.token_set_ratio(s1, s2) >= 75

        # Fit style matching (e.g. "classic fit" = "regular")
        if k == "fit":
            if "slim" in s1 and "slim" in s2:
                return True
            if any(x in s1 for x in ["relaxed", "loose"]) and any(x in s2 for x in ["relaxed", "loose"]):
                return True
            if any(x in s1 for x in ["regular", "classic", "standard"]) and any(x in s2 for x in ["regular", "classic", "standard"]):
                return True
            return s1 in s2 or s2 in s1 or fuzz.token_set_ratio(s1, s2) >= 80

        # Fallback to general token_set_ratio for other attributes
        return fuzz.token_set_ratio(s1, s2) >= 85

    return v1 == v2


def image_similarity(phash1: str | None, phash2: str | None) -> float:
    """Return similarity % based on pHash Hamming distance (0–100)."""
    if not phash1 or not phash2:
        return -1.0  # unknown
    try:
        h1 = imagehash.hex_to_hash(phash1)
        h2 = imagehash.hex_to_hash(phash2)
        distance = h1 - h2  # Hamming distance (0–64)
        return round((1 - distance / 64) * 100, 2)
    except Exception:
        return -1.0


def compute_diff(feat1: dict, feat2: dict) -> dict:
    """
    Compute full feature diff between link_1 and link_2 feature objects.
    Returns a match report dict.
    """
    fn1: dict = feat1.get("features_normalized", {})
    fn2: dict = feat2.get("features_normalized", {})

    # Align keys
    aligned_pairs = align_keys(list(fn1.keys()), list(fn2.keys()))

    feature_diff: dict[str, dict] = {}
    matched_keys: list[str] = []
    mismatched_keys: list[str] = []

    for k1, k2 in aligned_pairs:
        v1 = fn1[k1]
        v2 = fn2[k2]
        matched = values_match(k1, v1, v2)
        # Use the link_1 key as canonical key name in output
        feature_diff[k1] = {
            "link_1_key": k1,
            "link_2_key": k2,
            "link_1_value": v1,
            "link_2_value": v2,
            "match": matched,
        }
        if matched:
            matched_keys.append(k1)
        else:
            mismatched_keys.append(k1)

    # Keys only in link_1 (no counterpart in link_2)
    aligned_k1s = {p[0] for p in aligned_pairs}
    for k in fn1:
        if k not in aligned_k1s:
            feature_diff[k] = {
                "link_1_key": k, "link_2_key": None,
                "link_1_value": fn1[k], "link_2_value": None,
                "match": False,
            }
            mismatched_keys.append(k)

    # Keys only in link_2 (no counterpart in link_1)
    aligned_k2s = {p[1] for p in aligned_pairs}
    for k in fn2:
        if k not in aligned_k2s:
            feature_diff[k] = {
                "link_1_key": None, "link_2_key": k,
                "link_1_value": None, "link_2_value": fn2[k],
                "match": False,
            }
            mismatched_keys.append(k)

    # Title similarity
    title1 = feat1.get("title", "")
    title2 = feat2.get("title", "")
    title_sim = round(fuzz.token_set_ratio(title1, title2), 2) if title1 and title2 else -1.0

    # Image similarity (pHash)
    img_sim = image_similarity(feat1.get("primary_phash"), feat2.get("primary_phash"))

    brand1 = feat1.get("brand", "").strip()
    brand2 = feat2.get("brand", "").strip()

    return {
        "pair_id": feat1["pair_id"],
        "link_1_url": feat1.get("url", ""),
        "link_2_url": feat2.get("url", ""),
        "brand_link_1": brand1,
        "brand_link_2": brand2,
        "title_link_1": title1,
        "title_link_2": title2,
        "title_similarity_pct": title_sim,
        "image_similarity_pct": img_sim,
        "total_features_compared": len(aligned_pairs),
        "total_matched": len(matched_keys),
        "total_mismatched": len(mismatched_keys),
        "matched_keys": matched_keys,
        "mismatched_keys": mismatched_keys,
        "feature_diff": feature_diff,
    }


def run_matcher(pairs: list[dict]):
    done_ids = jsonl_index(DIFFS_OUT, "pair_id")

    feat1_map = {r["pair_id"]: r for r in jsonl_read(FEAT_LINK1)}
    feat2_map = {r["pair_id"]: r for r in jsonl_read(FEAT_LINK2)}

    pair_ids = [p["pair_id"] for p in pairs]
    logger.info(f"Matching {len(pair_ids)} pairs.")

    with Progress(SpinnerColumn(), TextColumn("[bold magenta]{task.description}"),
                  BarColumn(), TaskProgressColumn()) as progress:
        task = progress.add_task("Matching features...", total=len(pair_ids))

        for pair in pairs:
            pid = pair["pair_id"]
            if pid in done_ids:
                progress.advance(task)
                continue

            feat1 = feat1_map.get(pid)
            feat2 = feat2_map.get(pid)

            if not feat1 or not feat2:
                logger.warning(f"[{pid}] Missing feature data — skipping")
                progress.advance(task)
                continue

            diff = compute_diff(feat1, feat2)
            jsonl_append(DIFFS_OUT, diff)
            logger.info(
                f"[{pid}] features={diff['total_features_compared']} "
                f"matched={diff['total_matched']} "
                f"mismatched={diff['total_mismatched']} "
                f"title_sim={diff['title_similarity_pct']}% "
                f"img_sim={diff['image_similarity_pct']}%"
            )
            progress.advance(task)

    logger.info("Matching complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    pairs = load_pairs("data.csv", limit=args.limit)
    run_matcher(pairs)
