"""
pipeline.py — Orchestrator. Runs all steps end-to-end.

Usage:
    uv run python src/pipeline.py --step all
    uv run python src/pipeline.py --step scrape --limit 5
    uv run python src/pipeline.py --step extract --limit 5
    uv run python src/pipeline.py --step match --limit 5
    uv run python src/pipeline.py --step classify --limit 5
    uv run python src/pipeline.py --step all --limit 10 --workers 3
"""

from __future__ import annotations

import sys
import asyncio
import argparse
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from utils import get_logger, load_pairs

logger  = get_logger("pipeline")
console = Console()

STEPS = ["scrape", "extract", "match", "classify"]


def banner(text: str, color: str = "cyan"):
    console.print(Rule(f"[bold {color}]{text}[/bold {color}]", style=color))


def run_pipeline(step: str, limit: int | None, workers: int, csv_path: str):
    pairs = load_pairs(csv_path, limit=limit)
    console.print(Panel(
        f"[bold white]Product Comparison Pipeline[/bold white]\n"
        f"CSV Source: [yellow]{csv_path}[/yellow]  |  Step: [cyan]{step}[/cyan]  |  Pairs: [yellow]{len(pairs)}[/yellow]  |  Workers: [green]{workers}[/green]",
        border_style="bright_blue"
    ))

    steps_to_run = STEPS if step == "all" else [step]

    for s in steps_to_run:
        t0 = time.time()

        if s == "scrape":
            banner("STEP 0 — Web Scraping", "cyan")
            from step0_scraper import run_scraper
            asyncio.run(run_scraper(pairs, workers=workers))

        elif s == "extract":
            banner("STEP 1 — Feature Extraction + Normalization + Image Hashing", "purple")
            from step1_extractor import run_extractor
            run_extractor(pairs)

        elif s == "match":
            banner("STEP 2 — Matching Engine (Feature Diff)", "magenta")
            from step2_matcher import run_matcher
            run_matcher(pairs)

        elif s == "classify":
            banner("STEP 3 — Classification (Case A / B / C)", "yellow")
            from step3_classifier import run_classifier
            run_classifier(pairs)

        elapsed = time.time() - t0
        console.print(f"[green]✓ {s} done in {elapsed:.1f}s[/green]\n")

    console.print(Panel("[bold green]Pipeline complete![/bold green]", border_style="green"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Product Comparison Pipeline")
    parser.add_argument(
        "--step", choices=["all"] + STEPS, default="all",
        help="Which step to run (default: all)"
    )
    parser.add_argument("--limit",   type=int, default=None, help="Limit number of pairs")
    parser.add_argument("--workers", type=int, default=3,    help="Concurrent scraper workers")
    parser.add_argument("--csv",     type=str, default="data.csv", help="CSV path to load pairs from")
    args = parser.parse_args()

    run_pipeline(args.step, args.limit, args.workers, args.csv)
