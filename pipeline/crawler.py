"""
crawler.py — Fetch Hacker News mentions via the Algolia search API.

Design decisions:
- Raw JSON pages are persisted to data/raw/<topic>_page<N>.json AND to the
  raw_payloads table before any transformation happens.  This means a
  re-run that finds the file on disk skips the HTTP call entirely.
- Idempotency marker: if data/raw/<topic>_page<N>.json already exists we
  treat that page as done.  Simple, transparent, no extra state store.
- tenacity handles transient HTTP errors with exponential backoff so
  a brief API hiccup doesn't kill the run.
- A descriptive User-Agent is set per HN Algolia ToS best practice.
- All progress is logged; no bare print().
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from pipeline.db import get_connection, initialise_schema

logger = logging.getLogger(__name__)

# HN Algolia search endpoint — public, no auth required
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"

# Descriptive User-Agent so HN Algolia can identify our bot
USER_AGENT = (
    "hn-pipeline/1.0 (intern-assessment; "
    "contact: pipeline-bot@example.com)"
)


# ---------------------------------------------------------------------------
# HTTP layer with retry
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _fetch_page(session: requests.Session, topic: str, page: int, hits_per_page: int) -> dict:
    """
    Fetch one page of results for a topic from the Algolia API.

    Retries automatically on transient network errors (Timeout,
    ConnectionError) with exponential backoff.  HTTPError (4xx/5xx)
    is not retried — those are structural problems we should see.
    """
    params = {
        "query": topic,
        "hitsPerPage": hits_per_page,
        "page": page,
        # Fetch comments only.  Reasons:
        # 1. Comments always have `comment_text` filled in — link-only stories
        #    have an empty body, making sentiment classification unreliable.
        # 2. Each comment hit already includes `story_title` inline at no extra
        #    API cost, giving us parent context + the actual opinion together.
        # 3. Comments are where real user experience lives; story titles are
        #    neutral headlines written to be descriptive, not opinionated.
        # Algolia tag syntax: a bare string matches a single tag exactly.
        "tags": "comment",
    }
    logger.debug("GET %s params=%s", HN_SEARCH_URL, params)
    resp = session.get(HN_SEARCH_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Raw persistence helpers
# ---------------------------------------------------------------------------

def _raw_path(raw_dir: Path, topic: str, page: int) -> Path:
    """Canonical path for a raw JSON file."""
    safe_topic = topic.lower().replace(" ", "_")
    return raw_dir / f"{safe_topic}_page{page:03d}.json"


def _page_already_fetched(raw_dir: Path, topic: str, page: int) -> bool:
    """Return True if the raw file for this topic+page already exists on disk."""
    return _raw_path(raw_dir, topic, page).exists()


def _save_raw(raw_dir: Path, topic: str, page: int, payload: dict) -> None:
    """Persist the raw API response to disk."""
    path = _raw_path(raw_dir, topic, page)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.debug("Saved raw payload → %s", path)


def _save_raw_to_db(conn, topic: str, page: int, payload: dict, fetched_at: str) -> None:
    """
    Upsert the raw payload into the raw_payloads table.
    INSERT OR REPLACE so re-runs update the fetched_at timestamp.
    """
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO raw_payloads (topic, page, fetched_at, payload)
            VALUES (?, ?, ?, ?)
            """,
            (topic, page, fetched_at, json.dumps(payload)),
        )


# ---------------------------------------------------------------------------
# Main crawl function
# ---------------------------------------------------------------------------

def crawl(config: dict, db_path: Path, raw_dir: Path) -> list[dict]:
    """
    Crawl all configured topics and persist raw responses.

    Returns a flat list of raw hit dicts (one per HN item) for the
    transform step to process.  Items from already-fetched pages are
    loaded from disk rather than re-fetched.

    Args:
        config:   Parsed config.yaml contents.
        db_path:  Path to the SQLite database file.
        raw_dir:  Directory where raw JSON pages are saved.

    Returns:
        List of raw hit dicts ready for transform.
    """
    topics: list[str] = config["topics"]
    hits_per_page: int = config["crawler"]["hits_per_page"]
    pages_per_topic: int = config["crawler"]["pages_per_topic"]
    sleep_between_requests: float = config["crawler"]["sleep_seconds"]
    max_total: int = config["crawler"]["max_total_items"]

    conn = get_connection(db_path)
    initialise_schema(conn)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    all_hits: list[dict] = []
    total_fetched = 0
    skipped_pages = 0
    fetched_pages = 0

    logger.info(
        "Starting crawl: %d topics × %d pages × %d hits/page (cap=%d)",
        len(topics), pages_per_topic, hits_per_page, max_total,
    )

    for topic in topics:
        if total_fetched >= max_total:
            logger.info("Global cap of %d reached — stopping early.", max_total)
            break

        logger.info("── Topic: %r", topic)

        for page in range(pages_per_topic):
            if total_fetched >= max_total:
                break

            # --- Idempotency check: skip pages already on disk ---
            if _page_already_fetched(raw_dir, topic, page):
                logger.info(
                    "  page %d already fetched (found on disk) — loading from cache.",
                    page,
                )
                with open(_raw_path(raw_dir, topic, page), encoding="utf-8") as fh:
                    payload = json.load(fh)
                hits = payload.get("hits", [])
                # Tag cached hits with their topic (same as freshly fetched)
                for hit in hits:
                    hit["_topic"] = topic
                all_hits.extend(hits)
                total_fetched += len(hits)
                skipped_pages += 1
                continue

            # --- Fetch from API ---
            fetched_at = datetime.now(timezone.utc).isoformat()
            try:
                payload = _fetch_page(session, topic, page, hits_per_page)
            except Exception as exc:
                logger.error(
                    "Failed to fetch topic=%r page=%d after retries: %s",
                    topic, page, exc,
                )
                # Skip this page rather than crashing the whole run
                continue

            hits = payload.get("hits", [])
            if not hits:
                logger.info("  page %d returned 0 hits — stopping pagination.", page)
                break

            # Persist raw before any transformation
            _save_raw(raw_dir, topic, page, payload)
            _save_raw_to_db(conn, topic, page, payload, fetched_at)

            # Tag each hit with its search topic so transform.py knows which
            # topic produced this result (the JSON itself doesn't carry this).
            for hit in hits:
                hit["_topic"] = topic

            all_hits.extend(hits)
            total_fetched += len(hits)
            fetched_pages += 1

            logger.info(
                "  page %d: %d hits fetched (running total: %d)",
                page, len(hits), total_fetched,
            )

            # Throttle to be a polite API consumer
            if page < pages_per_topic - 1:
                logger.debug("  sleeping %.1fs before next request", sleep_between_requests)
                time.sleep(sleep_between_requests)

    conn.close()

    logger.info(
        "Crawl complete. pages_fetched=%d pages_from_cache=%d total_hits=%d",
        fetched_pages, skipped_pages, len(all_hits),
    )
    return all_hits
