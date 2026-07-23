"""
sample_for_eval.py — Randomly sample stored records for hand-labelling.

Usage:
    python scripts/sample_for_eval.py
    python scripts/sample_for_eval.py --config config.yaml --n 25

Outputs:
    outputs/eval_sample.xlsx  — Excel workbook with:
                                  • Read-only columns (grey background) for
                                    context: id, topic, author, title, text, url
                                  • manual_sentiment  — dropdown: positive /
                                    negative / neutral
                                  • manual_category   — dropdown: pricing /
                                    reliability / feature_request / comparison /
                                    integration / general
                                  • Frozen header row + auto-sized columns

Design decision:
    Pipeline's own sentiment/category labels are intentionally excluded so
    the user labels without anchoring bias.  evaluate.py fetches the pipeline
    labels directly from the DB at comparison time, keyed on `id`.

The sample is stratified by topic so every topic gets represented.
"""

import argparse
import logging
import random
import sys
from pathlib import Path

import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.db import get_connection

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Column definitions ─────────────────────────────────────────────────────
# (header_label, db_field, is_editable, approx_width)
COLUMNS = [
    ("id",                "id",           False, 14),
    ("topic",             "topic",        False, 18),
    ("author",            "author",       False, 14),
    ("title",             "title",        False, 45),
    ("text",              "text",         False, 60),
    ("url",               "url",          False, 40),
    ("created_at",        "created_at",   False, 22),
    ("manual_sentiment",  None,           True,  20),  # None = empty column, user fills
    ("manual_category",   None,           True,  22),
]

# Dropdown option lists
SENTIMENT_OPTIONS = ["positive", "negative", "neutral"]
CATEGORY_OPTIONS  = ["pricing", "reliability", "feature_request",
                     "comparison", "integration", "general"]

# Styles
_FILL_READONLY = PatternFill("solid", fgColor="EFEFEF")   # light grey — do not edit
_FILL_EDITABLE = PatternFill("solid", fgColor="FFFDE7")   # pale yellow — fill me in
_FILL_HEADER   = PatternFill("solid", fgColor="263238")   # dark slate header
_FONT_HEADER   = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
_FONT_BODY     = Font(name="Calibri", size=10)
_ALIGN_WRAP    = Alignment(wrap_text=True, vertical="top")
_ALIGN_CENTER  = Alignment(horizontal="center", vertical="top")

REGEN_HINT = (
    "To regenerate a clean eval sheet run:\n"
    "    python scripts/sample_for_eval.py"
)


# ── DB helpers ─────────────────────────────────────────────────────────────

def _fetch_stratified(conn, topics: list[str], n: int, seed: int) -> list[dict]:
    """Return n rows stratified by topic, shuffled."""
    per_topic = max(1, n // len(topics))
    remainder = n - per_topic * len(topics)
    rng = random.Random(seed)
    selected = []

    for topic in topics:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT id, topic, author, title, text, url, created_at "
                "FROM items WHERE topic = ?",
                (topic,),
            ).fetchall()
        ]
        k = min(per_topic, len(rows))
        selected.extend(rng.sample(rows, k))

    selected_ids = {r["id"] for r in selected}
    remaining = [
        dict(r) for r in conn.execute(
            "SELECT id, topic, author, title, text, url, created_at FROM items"
        ).fetchall()
        if r["id"] not in selected_ids
    ]
    if remainder > 0 and remaining:
        selected.extend(rng.sample(remaining, min(remainder, len(remaining))))

    rng.shuffle(selected)
    return selected


# ── Excel workbook builder ─────────────────────────────────────────────────

def _add_dropdown(ws, col_index: int, n_rows: int,
                  options: list[str], error_msg: str) -> None:
    """
    Attach an Excel Data Validation dropdown to a column.

    openpyxl's DataValidation.showDropDown is confusingly named:
      False → dropdown arrow IS shown  (what we want)
      True  → dropdown arrow is hidden
    The formula1 value must be a comma-separated list wrapped in double
    quotes and then in single quotes for the formula string.
    """
    col_letter = get_column_letter(col_index)
    cell_range = f"{col_letter}2:{col_letter}{n_rows + 1}"

    # Excel list formula: the options string must be <= 255 chars
    formula = '"' + ",".join(options) + '"'

    dv = DataValidation(
        type="list",
        formula1=formula,
        allow_blank=True,
        showDropDown=False,       # False = SHOW the arrow (inverted naming)
        showErrorMessage=True,
        errorTitle="Invalid value",
        error=error_msg,
        showInputMessage=True,
        promptTitle="Select a value",
        prompt=f"Choose one of: {', '.join(options)}",
    )
    ws.add_data_validation(dv)
    dv.sqref = cell_range


def build_workbook(rows: list[dict]) -> Workbook:
    """
    Build and return an openpyxl Workbook with:
      - Styled, frozen header row
      - Read-only columns in grey
      - Editable columns in pale yellow with dropdown validation
      - Auto-sized column widths
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Eval Sample"

    headers = [col[0] for col in COLUMNS]

    # ── Header row ─────────────────────────────────────────────────────────
    for c_idx, col in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=c_idx, value=col[0])
        cell.fill = _FILL_HEADER
        cell.font = _FONT_HEADER
        cell.alignment = _ALIGN_CENTER

    # Freeze the header so it stays visible while scrolling
    ws.freeze_panes = "A2"

    # ── Data rows ──────────────────────────────────────────────────────────
    for r_idx, row in enumerate(rows, start=2):
        for c_idx, (header, db_field, editable, width) in enumerate(COLUMNS, start=1):
            value = row.get(db_field, "") if db_field else ""
            cell = ws.cell(row=r_idx, column=c_idx, value=value)
            cell.font = _FONT_BODY
            cell.alignment = _ALIGN_WRAP
            cell.fill = _FILL_EDITABLE if editable else _FILL_READONLY

    # ── Dropdowns ──────────────────────────────────────────────────────────
    # Find column indices for the editable columns (1-based)
    col_names = [col[0] for col in COLUMNS]
    sent_col_idx = col_names.index("manual_sentiment") + 1
    cat_col_idx  = col_names.index("manual_category")  + 1

    _add_dropdown(
        ws, sent_col_idx, len(rows), SENTIMENT_OPTIONS,
        error_msg="Choose: positive, negative, or neutral",
    )
    _add_dropdown(
        ws, cat_col_idx, len(rows), CATEGORY_OPTIONS,
        error_msg="Choose one of the defined categories",
    )

    # ── Column widths ──────────────────────────────────────────────────────
    for c_idx, (header, db_field, editable, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(c_idx)].width = width

    # ── Row heights — taller rows for text columns ─────────────────────────
    ws.row_dimensions[1].height = 22       # header
    for r_idx in range(2, len(rows) + 2):
        ws.row_dimensions[r_idx].height = 55  # enough for wrapped text

    return wb


# ── Main ───────────────────────────────────────────────────────────────────

def sample(db_path: Path, n: int, seed: int = 42,
           output_path: Path | None = None) -> Path:
    """
    Draw n stratified records from the DB and write an xlsx eval sheet.
    """
    if not db_path.exists():
        logger.error(
            "Database not found at %s — run the pipeline first.\n%s",
            db_path, REGEN_HINT,
        )
        sys.exit(1)

    conn = get_connection(db_path)
    topics = [r["topic"] for r in conn.execute(
        "SELECT DISTINCT topic FROM items ORDER BY topic"
    ).fetchall()]

    if not topics:
        logger.error(
            "No topics found in database — run the pipeline first.\n%s", REGEN_HINT,
        )
        conn.close()
        sys.exit(1)

    rows = _fetch_stratified(conn, topics, n, seed)
    conn.close()

    if output_path is None:
        output_path = Path("outputs") / "eval_sample.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wb = build_workbook(rows)
    wb.save(output_path)

    logger.info("Wrote %d rows to %s", len(rows), output_path)
    logger.info(
        "Pipeline labels (sentiment/category) excluded — "
        "use the dropdowns to fill in manual_sentiment and manual_category."
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample records for hand-labelling")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--n", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("outputs/eval_sample.xlsx"))
    args = parser.parse_args()

    with open(args.config) as fh:
        config = yaml.safe_load(fh)

    db_path = Path(config["paths"]["db"])
    n = args.n or config["evaluation"]["sample_size"]

    out = sample(db_path, n, args.seed, args.output)
    print(f"\n✓ Hand-labelling workbook ready: {out}")
    print("  Grey columns  = context only, do not edit.")
    print("  Yellow columns = fill in using the dropdown in each cell:")
    print("    manual_sentiment  →  positive / negative / neutral")
    print("    manual_category   →  pricing / reliability / feature_request")
    print("                          comparison / integration / general")
    print("  Then run:  python scripts/evaluate.py\n")


if __name__ == "__main__":
    main()
