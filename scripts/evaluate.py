"""
evaluate.py — Compare hand-labels against pipeline classifier output.

Usage (after filling in outputs/eval_sample.xlsx):
    python scripts/evaluate.py
    python scripts/evaluate.py --file outputs/eval_sample.xlsx --config config.yaml

Accepts both .xlsx (default, from sample_for_eval.py) and .csv files.

How it works:
    1. Loads the hand-labelled file (which contains NO pipeline labels by design).
    2. Extracts the `id` column and queries the SQLite DB for the pipeline's
       sentiment + category for those exact rows.
    3. Compares manual vs. pipeline labels and reports accuracy, per-class
       precision/recall/F1, a confusion matrix, and error examples.

Robustness:
    Every structural problem (missing columns, no labels filled in, IDs not
    found in DB, wrong file) is caught and logged with an explicit error
    message and instructions on how to regenerate a clean eval sheet.

Reports:
    - Overall accuracy for sentiment and category
    - Per-class precision / recall / F1
    - Confusion matrix
    - Sample misclassified rows to spot error patterns
    - Known classifier failure modes
"""

import argparse
import csv
import logging
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from openpyxl import load_workbook

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.db import get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,   # keep logs on stderr so stdout is clean for report
)
logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

REGEN_HINT = (
    "\nTo regenerate a clean, unbiased eval sheet run:\n"
    "    python scripts/sample_for_eval.py\n"
    "Then fill in manual_sentiment and manual_category using the dropdowns "
    "and re-run this script."
)

# Columns that MUST exist in the file for evaluation to work
REQUIRED_COLUMNS = {"id", "manual_sentiment", "manual_category"}

# Valid label values — used to warn on obvious typos
VALID_SENTIMENTS = {"positive", "negative", "neutral"}
VALID_CATEGORIES = {
    "pricing", "reliability", "feature_request",
    "comparison", "integration", "general",
}


# ─── File loading (CSV or XLSX) ───────────────────────────────────────────────

def _load_xlsx(path: Path) -> list[dict]:
    """
    Read an xlsx workbook and return a list of row dicts.

    Reads the first sheet, treats row 1 as the header, and converts
    every subsequent row to a dict keyed by header name.
    Cell values are cast to str and stripped so downstream logic
    is identical for both csv and xlsx inputs.
    """
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        logger.error(
            "Could not open xlsx file %s: %s%s", path, exc, REGEN_HINT
        )
        sys.exit(1)

    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)

    try:
        headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
    except StopIteration:
        logger.error("xlsx file %s has no rows.%s", path, REGEN_HINT)
        sys.exit(1)

    result = []
    for raw_row in rows_iter:
        row = {
            headers[i]: (str(v).strip() if v is not None else "")
            for i, v in enumerate(raw_row)
            if i < len(headers)
        }
        result.append(row)

    wb.close()
    return result


def _load_csv(path: Path) -> list[dict]:
    """Read a plain CSV and return a list of row dicts."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except Exception as exc:
        logger.error(
            "Could not read CSV file %s: %s%s", path, exc, REGEN_HINT
        )
        sys.exit(1)


def load_and_validate_file(path: Path) -> list[dict]:
    """
    Load the hand-labelled file (.xlsx or .csv) and validate its structure.

    Raises SystemExit with a clear error message if any check fails.
    Returns a list of row dicts that have at least manual_sentiment filled in.
    """
    # ── File existence ────────────────────────────────────────────────────
    if not path.exists():
        logger.error(
            "File not found: %s\n"
            "Run the pipeline and generate the eval sheet first.%s",
            path, REGEN_HINT,
        )
        sys.exit(1)

    # ── Load by extension ─────────────────────────────────────────────────
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        raw_rows = _load_xlsx(path)
    elif suffix == ".csv":
        raw_rows = _load_csv(path)
    else:
        logger.error(
            "Unsupported file format: %r\n"
            "Expected .xlsx (default) or .csv.%s",
            suffix, REGEN_HINT,
        )
        sys.exit(1)

    if not raw_rows:
        logger.error("File %s is empty (no data rows).%s", path, REGEN_HINT)
        sys.exit(1)

    actual_columns = set(raw_rows[0].keys())

    # ── Required columns ──────────────────────────────────────────────────
    missing_cols = REQUIRED_COLUMNS - actual_columns
    if missing_cols:
        logger.error(
            "File is missing required column(s): %s\n"
            "Found columns: %s\n"
            "This usually means the wrong file was passed, or columns were\n"
            "accidentally deleted during editing.%s",
            sorted(missing_cols),
            sorted(actual_columns),
            REGEN_HINT,
        )
        sys.exit(1)

    # ── Detect pipeline-label columns that shouldn't be present ───────────
    pipeline_cols_present = {"sentiment", "category"} & actual_columns
    if pipeline_cols_present:
        logger.warning(
            "Column(s) %s found in file — these are pipeline labels and should "
            "not be present in the eval sheet (they can bias your answers).\n"
            "Evaluation will still proceed using the DB as the source of truth, "
            "but consider regenerating a clean sheet.%s",
            sorted(pipeline_cols_present), REGEN_HINT,
        )

    # ── At least some rows must be labelled ───────────────────────────────
    labelled_rows = [
        r for r in raw_rows
        if r.get("manual_sentiment", "").strip()
    ]
    if not labelled_rows:
        logger.error(
            "No rows with a filled-in manual_sentiment were found in %s.\n"
            "Open the file and fill in the 'manual_sentiment' column\n"
            "(valid values: positive, negative, neutral) then re-run.%s",
            path, REGEN_HINT,
        )
        sys.exit(1)

    # ── Warn on unrecognised label values (likely typos) ──────────────────
    bad_sentiments = {
        r["manual_sentiment"].strip().lower()
        for r in labelled_rows
        if r["manual_sentiment"].strip().lower() not in VALID_SENTIMENTS
    }
    if bad_sentiments:
        logger.warning(
            "Unrecognised manual_sentiment value(s): %s\n"
            "Expected one of: %s\n"
            "Rows with unrecognised values will be excluded from the report.",
            sorted(bad_sentiments), sorted(VALID_SENTIMENTS),
        )

    unlabelled_count = len(raw_rows) - len(labelled_rows)
    if unlabelled_count:
        logger.warning(
            "%d row(s) have no manual_sentiment and will be skipped.",
            unlabelled_count,
        )

    logger.info(
        "File validated (%s): %d total rows, %d labelled, %d skipped.",
        suffix, len(raw_rows), len(labelled_rows), unlabelled_count,
    )
    return labelled_rows


# ─── DB lookup ────────────────────────────────────────────────────────────────

def fetch_pipeline_labels(db_path: Path, ids: list[str]) -> dict[str, dict]:
    """
    Query the DB for the pipeline's sentiment + category for the given IDs.

    Returns {id: {"sentiment": ..., "category": ...}}.
    Warns (does not crash) for IDs not found in the DB.
    """
    if not db_path.exists():
        logger.error(
            "Database not found at %s\n"
            "Run the pipeline first, then re-run this script.%s",
            db_path, REGEN_HINT,
        )
        sys.exit(1)

    conn = get_connection(db_path)

    # SQLite has a limit on the size of IN clauses; chunk if needed
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, sentiment, category FROM items WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    conn.close()

    result = {r["id"]: {"sentiment": r["sentiment"], "category": r["category"]} for r in rows}

    missing_ids = set(ids) - result.keys()
    if missing_ids:
        logger.warning(
            "%d ID(s) from the CSV were not found in the database:\n  %s\n"
            "These rows will be excluded from the report.\n"
            "This can happen if the database was rebuilt after the eval sheet "
            "was generated.%s",
            len(missing_ids), "\n  ".join(sorted(missing_ids)[:10]), REGEN_HINT,
        )

    return result


# ─── Metrics ──────────────────────────────────────────────────────────────────

def accuracy(predicted: list[str], actual: list[str]) -> float:
    if not actual:
        return 0.0
    return sum(p == a for p, a in zip(predicted, actual)) / len(actual)


def confusion_matrix(predicted: list[str], actual: list[str], labels: list[str]) -> dict:
    """Return {true_label: {pred_label: count}} nested dict."""
    matrix: dict[str, dict[str, int]] = {lbl: defaultdict(int) for lbl in labels}
    for p, a in zip(predicted, actual):
        if a in matrix:
            matrix[a][p] += 1
    return matrix


def per_class_metrics(matrix: dict) -> dict:
    """Compute precision, recall, F1 per class from a confusion matrix."""
    results = {}
    for true_label, pred_counts in matrix.items():
        tp = pred_counts.get(true_label, 0)
        fp = sum(
            matrix[other].get(true_label, 0)
            for other in matrix if other != true_label
        )
        fn = sum(cnt for lbl, cnt in pred_counts.items() if lbl != true_label)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0 else 0.0
        )
        results[true_label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": tp + fn,
        }
    return results


def print_section(title: str) -> None:
    print(f"\n{'═' * 62}")
    print(f"  {title}")
    print(f"{'═' * 62}")


def print_report(
    label_type: str,
    pred: list[str],
    true: list[str],
    rows: list[dict],
    pred_col: str,
    true_col: str,
) -> None:
    """Print accuracy, per-class metrics, confusion matrix, and error examples."""
    acc = accuracy(pred, true)
    correct = sum(p == a for p, a in zip(pred, true))
    print(f"\n{label_type.upper()} accuracy: {acc:.1%}  ({correct}/{len(true)})\n")

    labels = sorted(set(true) | set(pred))
    matrix = confusion_matrix(pred, true, labels)
    metrics = per_class_metrics(matrix)

    # Per-class table
    col_w = max(len(l) for l in labels) + 2
    print(f"  {'Label':<{col_w}} {'Precision':>10} {'Recall':>8} {'F1':>8} {'Support':>8}")
    print(f"  {'-' * (col_w + 38)}")
    for lbl, m in sorted(metrics.items()):
        print(
            f"  {lbl:<{col_w}} {m['precision']:>10.2f}"
            f" {m['recall']:>8.2f} {m['f1']:>8.2f} {m['support']:>8}"
        )

    # Confusion matrix
    print(f"\n  Confusion matrix (rows = true label, cols = predicted):")
    header = f"  {'':>{col_w}}" + "".join(f"{l:>{col_w}}" for l in labels)
    print(header)
    for true_l in labels:
        row_str = f"  {true_l:>{col_w}}" + "".join(
            f"{matrix[true_l].get(p, 0):>{col_w}}" for p in labels
        )
        print(row_str)

    # Error examples
    errors = [
        r for r in rows
        if r.get(true_col, "").strip()
        and r.get(pred_col, "").strip()
        and r[pred_col].strip().lower() != r[true_col].strip().lower()
    ]
    if errors:
        n_show = min(8, len(errors))
        print(f"\n  Sample misclassifications (showing {n_show} of {len(errors)}):")
        for r in errors[:n_show]:
            snippet = (r.get("title") or r.get("text") or "")[:75]
            print(
                f"    pipeline={r[pred_col]!r:<14}  "
                f"manual={r[true_col]!r:<14}  "
                f"← {snippet!r}"
            )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate pipeline classifier against hand labels"
    )
    parser.add_argument(
        "--file", type=Path, default=Path("outputs/eval_sample.xlsx"),
        help="Path to the filled-in eval file (.xlsx or .csv, default: outputs/eval_sample.xlsx)",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"),
        help="Path to config.yaml (default: ./config.yaml)",
    )
    args = parser.parse_args()

    # ── Load config for DB path ───────────────────────────────────────────
    if not args.config.exists():
        logger.error("config.yaml not found at %s", args.config)
        sys.exit(1)
    with open(args.config) as fh:
        config = yaml.safe_load(fh)
    db_path = Path(config["paths"]["db"])

    # ── Load and validate the eval file (.xlsx or .csv) ──────────────────
    labelled_rows = load_and_validate_file(args.file)

    # ── Fetch pipeline labels from DB ─────────────────────────────────────
    ids = [r["id"] for r in labelled_rows]
    pipeline_labels = fetch_pipeline_labels(db_path, ids)

    # Merge pipeline labels back into rows; drop rows whose ID isn't in DB
    enriched: list[dict] = []
    for row in labelled_rows:
        pl = pipeline_labels.get(row["id"])
        if pl is None:
            continue   # already warned above
        row["pipeline_sentiment"] = (pl["sentiment"] or "").strip().lower()
        row["pipeline_category"]  = (pl["category"]  or "").strip().lower()
        row["manual_sentiment"]   = row["manual_sentiment"].strip().lower()
        row["manual_category"]    = row.get("manual_category", "").strip().lower()
        enriched.append(row)

    if not enriched:
        logger.error(
            "After joining the file with the database, no rows remain.\n"
            "This usually means the database was rebuilt since the eval sheet\n"
            "was generated and the IDs no longer match.%s",
            REGEN_HINT,
        )
        sys.exit(1)

    # ── Filter to valid sentiment values only ─────────────────────────────
    sent_rows = [
        r for r in enriched
        if r["manual_sentiment"] in VALID_SENTIMENTS
        and r["pipeline_sentiment"]
    ]
    cat_rows = [
        r for r in enriched
        if r.get("manual_category") in VALID_CATEGORIES
        and r.get("pipeline_category")
    ]

    # ── Print report ──────────────────────────────────────────────────────
    print_section(f"Evaluation Report — {len(enriched)} rows matched with DB")

    if sent_rows:
        pred_sent = [r["pipeline_sentiment"] for r in sent_rows]
        true_sent = [r["manual_sentiment"]   for r in sent_rows]
        print_report("Sentiment", pred_sent, true_sent, sent_rows,
                     "pipeline_sentiment", "manual_sentiment")
    else:
        logger.warning(
            "No rows with valid manual_sentiment values could be evaluated.\n"
            "Valid values: %s", sorted(VALID_SENTIMENTS),
        )

    if cat_rows:
        pred_cat = [r["pipeline_category"] for r in cat_rows]
        true_cat = [r["manual_category"]   for r in cat_rows]
        print_section("Category")
        print_report("Category", pred_cat, true_cat, cat_rows,
                     "pipeline_category", "manual_category")
    else:
        logger.warning(
            "No rows with valid manual_category values could be evaluated.\n"
            "Valid values: %s", sorted(VALID_CATEGORIES),
        )

    # ── Known failure modes ───────────────────────────────────────────────
    print_section("Known Classifier Limitations")
    print(
        "  • Sarcasm: VADER reads positive words as positive even when ironic\n"
        "  • Neutral/negative boundary: mildly frustrated short posts score near 0\n"
        "  • Category overlap: a pricing complaint may also mention an API\n"
        "  • Short / no-text items: single-word titles give VADER little signal\n"
        "  • `general` is the catch-all — high recall, zero precision\n"
    )


if __name__ == "__main__":
    main()
