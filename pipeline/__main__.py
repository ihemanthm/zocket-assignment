"""
__main__.py — Single entry point for the full pipeline.

Usage:
    python -m pipeline              # full run (crawl → transform → classify → summarise)
    python -m pipeline --skip-crawl # re-classify / re-summarise without re-fetching

Steps executed in order:
    1. Load config
    2. Initialise DB schema
    3. Crawl (idempotent — skips already-fetched pages)
    4. Transform + load (INSERT OR IGNORE deduplication)
    5. Classify (UPDATE sentiment + category for unclassified rows)
    6. Summarise (write outputs/summary.md)

Idempotency proof:
    Row counts are logged before and after each step so you can see
    that a second run produces zero new rows.
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

from pipeline.db import get_connection, initialise_schema, count_items, count_items_by_topic
from pipeline.crawler import crawl
from pipeline.transform import transform_and_load
from pipeline.classify import classify_all
from pipeline.summarize import generate_summary

# ---------------------------------------------------------------------------
# Logging setup — structured, timestamped, goes to stdout
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> dict:
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Main pipeline orchestration
# ---------------------------------------------------------------------------

def run_pipeline(config_path: Path, skip_crawl: bool = False, verbose: bool = False) -> None:
    _setup_logging(verbose)
    logger = logging.getLogger("pipeline")

    logger.info("═══════════════════════════════════════════")
    logger.info("  HN Pipeline — starting run")
    logger.info("═══════════════════════════════════════════")

    # ── Load config ────────────────────────────────────────────────────────
    config = _load_config(config_path)
    project_root = config_path.parent

    db_path = project_root / config["paths"]["db"]
    raw_dir = project_root / config["paths"]["raw_dir"]
    outputs_dir = project_root / config["paths"]["outputs_dir"]
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # ── Initialise schema ──────────────────────────────────────────────────
    conn = get_connection(db_path)
    initialise_schema(conn)

    # ── BEFORE counts (for idempotency proof) ─────────────────────────────
    before_total = count_items(conn)
    before_by_topic = count_items_by_topic(conn)
    conn.close()

    logger.info("BEFORE RUN — total rows: %d", before_total)
    if before_by_topic:
        for topic, n in before_by_topic.items():
            logger.info("  %-12s : %d rows", topic, n)
    else:
        logger.info("  (database is empty)")

    # ── Step 2: Crawl ──────────────────────────────────────────────────────
    if skip_crawl:
        logger.info("--skip-crawl set — skipping crawl step.")
        all_hits: list[dict] = []
        # Load all hits from raw files on disk so transform can still run
        for topic in config["topics"]:
            for page in range(config["crawler"]["pages_per_topic"]):
                safe = topic.lower().replace(" ", "_")
                path = raw_dir / f"{safe}_page{page:03d}.json"
                if path.exists():
                    import json
                    with open(path) as fh:
                        payload = json.load(fh)
                    for hit in payload.get("hits", []):
                        hit["_topic"] = topic
                    all_hits.extend(payload.get("hits", []))
    else:
        all_hits = crawl(config, db_path, raw_dir)
        # Tag each hit with its search topic for the transform step
        # (the crawler does this internally, but we do it here as a safety net)
        for hit in all_hits:
            if "_topic" not in hit:
                hit["_topic"] = hit.get("_search_topic", "unknown")

    # ── Step 3: Transform + Load ───────────────────────────────────────────
    logger.info("── Step 3: Transform + load")
    inserted = transform_and_load(all_hits, db_path)
    logger.info("Transform+load complete — %d new rows inserted.", inserted)

    # ── AFTER TRANSFORM counts ─────────────────────────────────────────────
    conn = get_connection(db_path)
    after_transform_total = count_items(conn)
    conn.close()

    logger.info(
        "AFTER TRANSFORM — total rows: %d (delta: %+d)",
        after_transform_total, after_transform_total - before_total,
    )

    # ── Step 5: Classify ──────────────────────────────────────────────────
    logger.info("── Step 5: Classify")
    classified = classify_all(db_path, config)
    logger.info("Classify complete — %d rows updated.", classified)

    # ── Step 7: Summarise ─────────────────────────────────────────────────
    logger.info("── Step 7: Summarise")
    summary_path = outputs_dir / "summary.md"
    generate_summary(db_path, summary_path, config)
    logger.info("Summary written → %s", summary_path)

    # ── Final counts — the idempotency proof ──────────────────────────────
    conn = get_connection(db_path)
    final_total = count_items(conn)
    final_by_topic = count_items_by_topic(conn)
    conn.close()

    logger.info("═══════════════════════════════════════════")
    logger.info("  Pipeline complete")
    logger.info("  BEFORE: %d rows │ AFTER: %d rows │ DELTA: %+d",
                before_total, final_total, final_total - before_total)
    logger.info("  Per-topic breakdown:")
    for topic, n in final_by_topic.items():
        logger.info("    %-12s : %d rows", topic, n)
    logger.info("═══════════════════════════════════════════")

    if final_total == before_total and before_total > 0:
        logger.info("✓ Idempotency confirmed — second run produced 0 new rows.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HN Pipeline — crawl → transform → classify → summarise"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--skip-crawl",
        action="store_true",
        help="Skip the crawl step; use already-cached raw files",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()
    run_pipeline(args.config, skip_crawl=args.skip_crawl, verbose=args.verbose)


if __name__ == "__main__":
    main()
