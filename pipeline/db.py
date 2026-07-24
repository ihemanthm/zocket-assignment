"""
db.py — Schema definition and SQLite helpers.

Design decisions:
- id is HN's own objectID (string), used as PRIMARY KEY.  This gives us
  stable, globally-unique keys without any UUID generation.
- INSERT OR IGNORE handles all deduplication: re-runs simply skip rows
  that already exist.
- sentiment and category are nullable on insert; the classify step fills
  them with a separate UPDATE so the two concerns stay decoupled.
- A single get_connection() factory keeps WAL-mode and foreign-key
  pragma setup in one place.
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

CREATE_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,   -- HN objectID, e.g. "40123456"
    topic       TEXT NOT NULL,      -- which search topic produced this row
    source      TEXT NOT NULL,      -- always "hn_algolia" for now
    author      TEXT,               -- story/comment author; NULL → "unknown"
    title       TEXT,               -- story title or first 120 chars of comment
    text        TEXT,               -- body text (NULL for link-only stories)
    url         TEXT,               -- external URL if present
    created_at  TEXT NOT NULL,      -- ISO-8601 from HN (created_at field)
    fetched_at  TEXT NOT NULL,      -- ISO-8601 UTC when we fetched it
    sentiment   TEXT,               -- filled by classify step
    category    TEXT                -- filled by classify step
);
"""

# Index to speed up per-topic queries used by the summariser
CREATE_TOPIC_INDEX = """
CREATE INDEX IF NOT EXISTS idx_items_topic ON items (topic);
"""

# Store raw API responses so re-runs never need to re-fetch
CREATE_RAW_PAYLOADS_TABLE = """
CREATE TABLE IF NOT EXISTS raw_payloads (
    topic       TEXT NOT NULL,
    page        INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,      -- full JSON blob as a string
    PRIMARY KEY (topic, page)
);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database and return a connection.

    WAL mode is enabled so reads don't block writes — useful if we ever
    run crawler and classifier concurrently.  row_factory = Row lets
    callers access columns by name.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # WAL mode: better concurrency, no performance downside for our scale
    conn.execute("PRAGMA journal_mode=WAL;")
    # Enforce FK constraints (not used yet, but good hygiene)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def initialise_schema(conn: sqlite3.Connection) -> None:
    """
    Create all tables and indexes if they don't already exist.
    Idempotent — safe to call on every startup.
    """
    with conn:
        conn.execute(CREATE_ITEMS_TABLE)
        conn.execute(CREATE_RAW_PAYLOADS_TABLE)
        conn.execute(CREATE_TOPIC_INDEX)
    logger.info("Schema initialised (tables and indexes are up-to-date).")


def count_items(conn: sqlite3.Connection) -> int:
    """Return total number of rows in the items table."""
    row = conn.execute("SELECT COUNT(*) FROM items").fetchone()
    return row[0]


def count_items_by_topic(conn: sqlite3.Connection) -> dict:
    """Return {topic: count} mapping."""
    rows = conn.execute(
        "SELECT topic, COUNT(*) as n FROM items GROUP BY topic ORDER BY topic"
    ).fetchall()
    return {r["topic"]: r["n"] for r in rows}
