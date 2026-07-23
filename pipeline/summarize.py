"""
summarize.py — Generate a Markdown summary of pipeline results.

Outputs to outputs/summary.md after every run.  The summary includes:
- Total rows crawled and deduped
- Counts by category (across all topics)
- Sentiment breakdown per topic
- Top N items per topic (by created_at, most recent first)
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from pipeline.db import get_connection

logger = logging.getLogger(__name__)


def generate_summary(db_path: Path, output_path: Path, config: dict) -> None:
    """
    Query the database and write a Markdown summary to output_path.
    """
    top_n = config["summary"]["top_items_per_topic"]
    conn = get_connection(db_path)

    # ── Overall totals ──────────────────────────────────────────────────────
    total_rows = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    topics_covered = conn.execute(
        "SELECT COUNT(DISTINCT topic) FROM items"
    ).fetchone()[0]

    # ── Category breakdown ─────────────────────────────────────────────────
    category_counts = conn.execute(
        """
        SELECT category, COUNT(*) AS n
        FROM items
        GROUP BY category
        ORDER BY n DESC
        """
    ).fetchall()

    # ── Sentiment breakdown per topic ──────────────────────────────────────
    sentiment_by_topic = conn.execute(
        """
        SELECT topic, sentiment, COUNT(*) AS n
        FROM items
        GROUP BY topic, sentiment
        ORDER BY topic, sentiment
        """
    ).fetchall()

    # Restructure: {topic: {sentiment: count}}
    topic_sentiment: dict[str, dict[str, int]] = {}
    for row in sentiment_by_topic:
        t, s, n = row["topic"], row["sentiment"] or "unclassified", row["n"]
        topic_sentiment.setdefault(t, {})[s] = n

    # ── Top items per topic ────────────────────────────────────────────────
    topics = [r["topic"] for r in conn.execute(
        "SELECT DISTINCT topic FROM items ORDER BY topic"
    ).fetchall()]

    top_items: dict[str, list] = {}
    for topic in topics:
        rows = conn.execute(
            """
            SELECT id, title, author, url, created_at, sentiment, category
            FROM items
            WHERE topic = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (topic, top_n),
        ).fetchall()
        top_items[topic] = [dict(r) for r in rows]

    conn.close()

    # ── Render Markdown ────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    lines += [
        f"# HN Pipeline — Summary Report",
        f"",
        f"_Generated: {now}_",
        f"",
        f"---",
        f"",
        f"## Overview",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total items stored | {total_rows:,} |",
        f"| Topics covered | {topics_covered} |",
        f"",
    ]

    # Category breakdown table
    lines += [
        f"## Category Breakdown (all topics combined)",
        f"",
        f"| Category | Count | % |",
        f"|----------|-------|---|",
    ]
    for row in category_counts:
        pct = (row["n"] / total_rows * 100) if total_rows else 0
        lines.append(f"| {row['category'] or 'unclassified'} | {row['n']:,} | {pct:.1f}% |")
    lines.append("")

    # Sentiment per topic
    lines += [
        f"## Sentiment Breakdown by Topic",
        f"",
        f"| Topic | Positive | Neutral | Negative | Unclassified |",
        f"|-------|----------|---------|----------|--------------|",
    ]
    for topic in sorted(topic_sentiment.keys()):
        s = topic_sentiment[topic]
        lines.append(
            f"| {topic} "
            f"| {s.get('positive', 0)} "
            f"| {s.get('neutral', 0)} "
            f"| {s.get('negative', 0)} "
            f"| {s.get('unclassified', 0)} |"
        )
    lines.append("")

    # Top items per topic
    lines += [f"## Top {top_n} Items per Topic (most recent)", ""]
    for topic in sorted(top_items.keys()):
        lines.append(f"### {topic}")
        lines.append("")
        for i, item in enumerate(top_items[topic], 1):
            title = item["title"] or "(no title)"
            url = item["url"] or f"https://news.ycombinator.com/item?id={item['id']}"
            lines.append(f"{i}. **[{title}]({url})**")
            lines.append(
                f"   - Author: `{item['author']}` | "
                f"Sentiment: `{item['sentiment'] or '?'}` | "
                f"Category: `{item['category'] or '?'}` | "
                f"Posted: {(item['created_at'] or '')[:10]}"
            )
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Summary written to %s (%d lines)", output_path, len(lines))
