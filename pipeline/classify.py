"""
classify.py — Sentiment + category classification for stored HN items.

Design philosophy (transparency over accuracy):
- Sentiment uses VADER (Valence Aware Dictionary and sEntiment Reasoner),
  a lexicon-based method built for short, informal social text.  No model
  training, no API calls, fully explainable: each word has a pre-assigned
  score and the compound score is their weighted sum.
- Category uses keyword matching against a small, hand-designed taxonomy.
  Again: fully transparent — you can read exactly why a record was bucketed.

Taxonomy (6 categories):
    pricing        — cost, subscription, free tier, billing
    reliability    — downtime, bugs, performance, speed, latency
    feature_request — asks for new features, missing functionality
    comparison     — comparing tools, switching, migration
    integration    — APIs, plugins, connections to other tools
    general        — anything that doesn't match the above

VADER threshold:
    compound >= +0.05 → positive
    compound <= -0.05 → negative
    between           → neutral
(Standard VADER thresholds from Hutto & Gilbert 2014)

The classify step only touches rows where sentiment IS NULL, so re-runs
are idempotent and don't re-classify already-processed rows.
"""

import logging
import re
from pathlib import Path

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VADER analyser (initialise once — loading the lexicon takes ~100ms)
# ---------------------------------------------------------------------------
_analyser = SentimentIntensityAnalyzer()


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------

def _get_sentiment(text: str, pos_thresh: float, neg_thresh: float) -> str:
    """
    Run VADER on text and return 'positive', 'negative', or 'neutral'.

    We concatenate title + body for richer signal.  VADER handles None
    gracefully but we guard anyway.
    """
    if not text or not text.strip():
        return "neutral"
    scores = _analyser.polarity_scores(text)
    compound = scores["compound"]
    if compound >= pos_thresh:
        return "positive"
    if compound <= neg_thresh:
        return "negative"
    return "neutral"


# ---------------------------------------------------------------------------
# Category — keyword taxonomy
# ---------------------------------------------------------------------------

# Each category maps to a list of keyword patterns (plain strings, matched
# case-insensitively as whole words / substrings).  Earlier entries in the
# ORDERED list take priority when multiple categories match.
#
# Design note: using a list of tuples (not a dict) so that priority order
# is explicit and deterministic — Python dicts preserve insertion order
# since 3.7 but a list makes the intent clearer.

_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("pricing", [
        "price", "pricing", "cost", "expensive", "cheap", "afford",
        "free tier", "free plan", "subscription", "billing", "invoice",
        "pay", "paid", "charge", "revenue", "plan", "tier",
    ]),
    ("reliability", [
        "down", "outage", "incident", "slow", "lag", "latency", "timeout",
        "crash", "bug", "broken", "issue", "problem", "error", "fail",
        "performance", "uptime", "downtime", "unstable", "unreliable",
    ]),
    ("feature_request", [
        "wish", "would love", "please add", "missing", "lack", "need",
        "feature request", "roadmap", "when will", "support for",
        "add support", "request", "suggestion", "hoping for",
    ]),
    ("comparison", [
        "vs", "versus", "compared to", "compare", "alternative",
        "switched", "migrated", "migration", "instead of", "better than",
        "worse than", "replace", "competitor", "similar to",
    ]),
    ("integration", [
        "api", "webhook", "plugin", "extension", "sdk", "integration",
        "connect", "import", "export", "sync", "embed", "zapier",
        "third-party", "oauth", "rest api", "graphql", "endpoint",
    ]),
]

_DEFAULT_CATEGORY = "general"


def _get_category(text: str) -> str:
    """
    Assign a category by scanning text for keyword matches.

    We normalise to lowercase once and then check for each keyword with a
    simple `in` test.  This is O(len(text) × total_keywords) — fast enough
    for our scale and completely transparent.
    """
    if not text:
        return _DEFAULT_CATEGORY

    lower_text = text.lower()

    for category, keywords in _CATEGORY_RULES:
        for kw in keywords:
            # Use word-boundary-aware check for single words to avoid
            # false positives (e.g. "pay" inside "display").
            if " " in kw:
                # Multi-word phrase: plain substring match is fine
                if kw in lower_text:
                    return category
            else:
                # Single word: require non-alphanumeric boundaries
                if re.search(r"\b" + re.escape(kw) + r"\b", lower_text):
                    return category

    return _DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# Main classify function
# ---------------------------------------------------------------------------

def classify_all(db_path: Path, config: dict) -> int:
    """
    Classify all items that have no sentiment yet.

    Only updates rows where sentiment IS NULL so re-runs are idempotent
    and don't overwrite manual corrections.

    Returns the number of rows updated.
    """
    pos_thresh = config["classifier"]["sentiment_positive_threshold"]
    neg_thresh = config["classifier"]["sentiment_negative_threshold"]

    conn = get_connection(db_path)

    # Fetch only unclassified rows to avoid re-processing
    rows = conn.execute(
        "SELECT id, title, text FROM items WHERE sentiment IS NULL"
    ).fetchall()

    if not rows:
        logger.info("No unclassified rows found — classify step is a no-op.")
        conn.close()
        return 0

    logger.info("Classifying %d unclassified rows…", len(rows))

    updates: list[tuple[str, str, str]] = []
    for row in rows:
        # Combine title + text for richer signal; handle NULLs gracefully
        combined = " ".join(filter(None, [row["title"], row["text"]])).strip()
        sentiment = _get_sentiment(combined, pos_thresh, neg_thresh)
        category = _get_category(combined)
        updates.append((sentiment, category, row["id"]))

    with conn:
        conn.executemany(
            "UPDATE items SET sentiment = ?, category = ? WHERE id = ?",
            updates,
        )

    conn.close()
    logger.info("Classified %d rows.", len(updates))
    return len(updates)
