"""
utils.py — Shared helpers: logging, JSONL I/O, retry, CSV loader, URL utils.
"""

from __future__ import annotations

import csv
import json
import logging
import time
import functools
from pathlib import Path
from typing import Any, Generator, Callable

from rich.logging import RichHandler
from rich.console import Console

console = Console()

# ── Logging ──────────────────────────────────────────────────────────────────

def get_logger(name: str, log_file: str = "logs/pipeline.log") -> logging.Logger:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    # Console handler (rich)
    ch = RichHandler(console=console, rich_tracebacks=True, markup=True)
    ch.setLevel(logging.INFO)

    # File handler
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s"))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


# ── Retry decorator ───────────────────────────────────────────────────────────

def retry(max_attempts: int = 3, base_delay: float = 2.0, exceptions=(Exception,)):
    """Exponential backoff retry decorator."""
    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        raise
                    delay = base_delay * (2 ** (attempt - 1))
                    logging.getLogger("retry").warning(
                        f"[{fn.__name__}] attempt {attempt}/{max_attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
        return wrapper
    return decorator


def async_retry(max_attempts: int = 3, base_delay: float = 2.0, exceptions=(Exception,)):
    """Async exponential backoff retry decorator."""
    import asyncio
    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        raise
                    delay = base_delay * (2 ** (attempt - 1))
                    logging.getLogger("retry").warning(
                        f"[{fn.__name__}] attempt {attempt}/{max_attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
        return wrapper
    return decorator


# ── JSONL I/O ─────────────────────────────────────────────────────────────────

def jsonl_read(path: str | Path) -> list[dict]:
    """Read all records from a JSONL file. Returns [] if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return []
    records = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def jsonl_append(path: str | Path, record: dict) -> None:
    """Append a single record to a JSONL file (creates if not exists)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def jsonl_index(path: str | Path, key: str) -> set:
    """Return a set of all values for `key` in an existing JSONL file."""
    return {r[key] for r in jsonl_read(path) if key in r}


def jsonl_index_composite(path: str | Path, keys: list[str]) -> set[tuple]:
    """Return a set of tuples for composite key lookup."""
    result = set()
    for r in jsonl_read(path):
        vals = tuple(r.get(k) for k in keys)
        if all(v is not None for v in vals):
            result.add(vals)
    return result


# ── CSV Loader ────────────────────────────────────────────────────────────────

def load_pairs(csv_path: str | Path, limit: int | None = None) -> list[dict]:
    """
    Load data.csv and return list of pair dicts.
    Each dict: { pair_id, link_1_url, link_2_url, meta: {...} }
    """
    pairs = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit and i >= limit:
                break
            # Clean Item_Id (may have extra quotes)
            item_id = row["Item_Id"].strip().strip('"')
            link_1_url = f"https://www.walmart.com/ip/{item_id}"
            link_2_url = row["Comp_Url"].strip()
            pair_id = f"row_{i+1:04d}"
            pairs.append({
                "pair_id": pair_id,
                "link_1_url": link_1_url,
                "link_2_url": link_2_url,
                "meta": {
                    "month": row.get("Month", ""),
                    "item_id": item_id,
                    "original_match_type": row.get("Match_Type", ""),
                    "super_department": row.get("Super_Department", ""),
                    "department": row.get("Department", ""),
                    "product_type": row.get("Product_Type", ""),
                }
            })
    return pairs
