"""
transform.py — Normalize raw HN Algolia hits and load them into SQLite.

Design decisions:
- INSERT OR IGNORE on the PRIMARY KEY (id) is all the deduplication we
  need.  If a row already exists, the insert is a silent no-op.  This
  means sentiment/category set by the classify step are never clobbered
  by a re-run of the transform step.
- All text fields pass through clean_html() before storage so the DB
  never contains raw HTML markup or entity sequences.  The HN Algolia
  API returns comment_text as rendered HTML (e.g. &gt;, <p>, <i>, <a>)
  which is unreadable as-is and misleads VADER's token scorer.
- Missing fields are handled gracefully: author → "unknown", title falls
  back to first 120 chars of the comment_text field, etc.
- created_at is taken from HN's own timestamp, converted to ISO-8601 so
  it's both human-readable and SQLite-sortable.
- fetched_at is the wall-clock time we ran this transform, distinct from
  created_at (when the HN item was posted).
"""

import html
import logging
import re
from datetime import datetime, timezone

from pipeline.db import get_connection, initialise_schema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _safe_str(value, default: str = "") -> str:
    """Return str(value) stripped, or default if value is None/empty."""
    if value is None:
        return default
    s = str(value).strip()
    return s if s else default


def _truncate(text: str, max_chars: int = 120) -> str:
    """Truncate a string to max_chars, appending '…' if cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


# Matches any HTML tag: opening, closing, or self-closing.
_TAG_RE = re.compile(r"<[^>]+>")


def clean_html(raw: str) -> str:
    """
    Strip HTML markup and decode entities from HN comment/title text.

    The HN Algolia API returns comment_text as raw HTML, for example:
        &gt; <i>&quot;great product&quot;</i><p>Agreed, works well.<p>
        <a href="https://...">link text</a>

    We clean in three ordered passes:
      1. Replace <p> and <br> tags with newlines so paragraph breaks are
         preserved as whitespace rather than words running together.
      2. Strip all remaining HTML tags (keep their visible text content).
      3. Decode HTML entities (&gt; → >, &#x27; → ', &quot; → ", etc.)
         using the stdlib html module — handles both named and numeric forms.
      4. Collapse runs of whitespace into single spaces and strip edges.

    Uses only stdlib (html + re) — no new dependencies.
    """
    if not raw:
        return raw

    # Pass 1: paragraph/line breaks → newline so words don't merge
    text = re.sub(r"<br\s*/?>|<p\s*/?>|</p>", "\n", raw, flags=re.IGNORECASE)

    # Pass 2: strip all remaining tags, keeping their inner text
    text = _TAG_RE.sub("", text)

    # Pass 3: decode HTML entities  (&gt; &#x27; &quot; &amp; etc.)
    text = html.unescape(text)

    # Pass 4: normalise whitespace — collapse newlines/tabs/spaces to one space
    text = " ".join(text.split())

    return text.strip()


def _normalise_hit(hit: dict, topic: str) -> dict | None:
    """
    Convert one raw Algolia hit dict into a row ready for insertion.

    We now fetch comments only, so the field mapping is:
      title  ←  story_title  (parent story’s headline — the context)
      text   ←  comment_text (the commenter’s words — the opinion)
      url    ←  story_url → url → HN comment permalink (always resolvable)

    Keeping story_title as the ‘title’ means the classify step sees:
      “<what the story was about> <what the user said about it>”
    which gives VADER and the keyword rules far richer signal than a
    comment body alone.

    Returns None if the hit lacks an objectID (we can’t deduplicate it).
    """
    obj_id = hit.get("objectID")
    if not obj_id:
        logger.warning("Hit missing objectID — skipping: %s", hit)
        return None

    # Title: for comments this is the parent story's headline, giving context.
    # For stories (kept for backwards-compat) it's the story's own title.
    # Clean HTML entities/tags — story titles occasionally contain &amp; etc.
    raw_title = clean_html(
        _safe_str(hit.get("story_title"))   # parent story title (comments)
        or _safe_str(hit.get("title"))       # story’s own title (stories)
    )

    # Body text: comment_text for comments (always filled), story_text for
    # self-post stories (Ask HN / Show HN).  Prioritise comment_text because
    # that’s the actual opinion we want to classify.
    # clean_html() strips <p>, <i>, <a> tags and decodes &gt; &#x27; etc.
    raw_text = clean_html(
        _safe_str(hit.get("comment_text"))   # comment body — our primary signal
        or _safe_str(hit.get("story_text"))  # self-post body (fallback)
    )

    # If there's still no title, synthesise one from the first line of body
    if not raw_title and raw_text:
        raw_title = _truncate(raw_text)

    # URL resolution order for comments:
    #   1. story_url  — external link the parent story points to (most useful)
    #   2. url        — present on stories, sometimes on comments
    #   3. HN permalink for the comment itself — always resolvable, never None
    obj_id = hit.get("objectID")
    resolved_url = (
        _safe_str(hit.get("story_url"))
        or _safe_str(hit.get("url"))
        or f"https://news.ycombinator.com/item?id={obj_id}"
    )

    # created_at_i is a UNIX timestamp; convert to ISO-8601
    ts_unix = hit.get("created_at_i")
    if ts_unix:
        created_at = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()
    else:
        created_at = _safe_str(hit.get("created_at"), default="1970-01-01T00:00:00+00:00")

    fetched_at = datetime.now(timezone.utc).isoformat()

    return {
        "id": str(obj_id),
        "topic": topic,
        "source": "hn_algolia",
        "author": _safe_str(hit.get("author"), default="unknown"),
        "title": raw_title or None,
        "text": raw_text or None,
        "url": resolved_url,
        "created_at": created_at,
        "fetched_at": fetched_at,
        "sentiment": None,   # filled by classify step
        "category": None,    # filled by classify step
    }


# ---------------------------------------------------------------------------
# Bulk insert
# ---------------------------------------------------------------------------

INSERT_SQL = """
INSERT OR IGNORE INTO items
    (id, topic, source, author, title, text, url, created_at, fetched_at,
     sentiment, category)
VALUES
    (:id, :topic, :source, :author, :title, :text, :url, :created_at,
     :fetched_at, :sentiment, :category)
"""


def transform_and_load(hits: list[dict], db_path, topic_for_each_hit: str | None = None) -> int:
    """
    Normalize raw hits and bulk-insert them into the items table.

    Each hit must have a `_topic` key injected by the caller (we do this
    in __main__.py), OR topic_for_each_hit can be provided as a fallback.

    Returns the number of rows actually inserted (ignoring duplicates).
    """
    conn = get_connection(db_path)
    initialise_schema(conn)

    rows = []
    skipped = 0
    for hit in hits:
        # The topic is embedded by the crawler step into each hit dict
        topic = hit.get("_topic") or topic_for_each_hit or "unknown"
        row = _normalise_hit(hit, topic)
        if row is None:
            skipped += 1
            continue
        rows.append(row)

    if not rows:
        logger.warning("No valid rows to insert (skipped=%d).", skipped)
        conn.close()
        return 0

    # Batch insert — INSERT OR IGNORE handles all deduplication silently
    with conn:
        conn.executemany(INSERT_SQL, rows)

    # SQLite doesn't easily report "rows inserted vs. ignored" from executemany,
    # so we read the change count from the connection.
    inserted = conn.total_changes
    logger.info(
        "Transform+load: %d rows attempted, %d inserted, %d ignored (duplicates), "
        "%d normalisation failures",
        len(rows), inserted, len(rows) - inserted, skipped,
    )

    conn.close()
    return inserted
