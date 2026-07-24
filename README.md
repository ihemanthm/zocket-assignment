# HN Pipeline — Intern Assessment

A config-driven, idempotent data pipeline that crawls Hacker News discussions about five configured topics, classifies each item with sentiment and a category, and emits a summary report.

---

## Setup & Run

**Prerequisites:** Python 3.10+

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd zocket-assignment

# 2. Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Run the full pipeline
python -m pipeline

# Or with Make:
make run
```

That's it. The pipeline will:
1. Crawl HN Algolia for all 5 configured topics
2. Store raw JSON pages to `data/raw/`
3. Normalize + load into `data/hn_pipeline.db`
4. Classify sentiment + category
5. Write `outputs/summary.md`

**Run it a second time** to verify idempotency — it will be a near-instant no-op:

```bash
python -m pipeline   # or: make run-again
```

You'll see in the logs:
```
BEFORE: 500 rows │ AFTER: 500 rows │ DELTA: +0
✓ Idempotency confirmed — second run produced 0 new rows.
```

### Other commands

```bash
python -m pipeline --skip-crawl           # re-classify / re-summarise without re-fetching
make sample-eval                          # Generate outputs/eval_sample.xlsx for hand-labelling
make evaluate                             # Compare hand labels to classifier (after filling the xlsx)
make clean                                # Remove database + outputs (keeps raw cache)
make clean-all                            # Remove everything including raw cache
```

---

## Project Structure

```
├── config.yaml          # All tunable knobs (topics, rate limits, thresholds)
├── pipeline/
│   ├── __main__.py      # Entry point: python -m pipeline
│   ├── db.py            # Schema + SQLite helpers
│   ├── crawler.py       # Crawl HN Algolia → persist raw JSON
│   ├── transform.py     # Normalize raw hits → INSERT OR IGNORE
│   ├── classify.py      # VADER sentiment + keyword category
│   └── summarize.py     # Markdown summary generator
├── scripts/
│   ├── sample_for_eval.py  # Stratified sampling for hand-labelling
│   └── evaluate.py         # Accuracy report vs. hand labels
├── data/
│   ├── raw/             # Raw JSON pages (idempotency cache)
│   └── hn_pipeline.db   # SQLite database
└── outputs/
    ├── summary.md       # Auto-generated on every run
    └── eval_sample.xlsx  # Hand-labelling worksheet (Step 6)
```

---

## Design Decisions & Tradeoffs

### Crawl strategy
- **Algolia HN API** (`hn.algolia.com/api/v1/search`) — no auth, ToS-compliant, stable.
- **Raw-first persistence:** raw JSON pages are saved to disk *before* any transformation. This means:
  - A crash mid-pipeline doesn't lose fetched data.
  - Re-runs load from disk in milliseconds; no HTTP calls made.
- **Idempotency marker:** the presence of `data/raw/<topic>_pageNNN.json` is the marker. Simple, visible, no hidden state.
- **Cap at 500 items** per the spec — roughly 2 pages × 50 hits × 5 topics hit the cap naturally.
- **Throttle:** 1 second sleep between paginated requests. Configurable in `config.yaml`.
- **Retry:** `tenacity` with exponential backoff on `Timeout` / `ConnectionError`. `HTTPError` is not retried (structural problems shouldn't be hidden).

### Schema
- `id` is HN's own `objectID` — a stable, globally unique string. Using it as `PRIMARY KEY` gives free deduplication.
- `INSERT OR IGNORE` is the entire deduplication strategy. No hashing, no external state.
- `sentiment` and `category` are `NULL` on insert; the classify step fills them with an `UPDATE`. This decouples the two concerns: you can re-classify without re-crawling.

### Classifier

#### Sentiment — VADER
- [VADER](https://github.com/cjhutto/vaderSentiment) (Valence Aware Dictionary and sEntiment Reasoner) is a lexicon-based model built specifically for social text.
- **Why VADER over a neural model:** fully transparent (every word has a published score), no training, no API, runs in microseconds, and the compound score formula is [published in a paper](https://ojs.aaai.org/index.php/ICWSM/article/view/14550).
- Thresholds (configurable in `config.yaml`): `compound >= 0.05` → positive, `<= -0.05` → negative, between → neutral. These are the standard thresholds from Hutto & Gilbert 2014.
- Input: title + body text concatenated (more signal than either alone).

#### Category — Keyword rules
Six categories defined in `classify.py`:

| Category | Keywords (sample) |
|---|---|
| `pricing` | price, cost, expensive, free tier, billing |
| `reliability` | down, outage, bug, crash, slow, latency |
| `feature_request` | wish, missing, roadmap, please add |
| `comparison` | vs, alternative, switched, migration |
| `integration` | api, webhook, plugin, sdk, sync |
| `general` | (catch-all) |

- **Why keyword rules over a classifier:** zero training data needed, every decision is auditable ("it matched `outage` → reliability"), and edge cases are fixable by adding a word.
- Priority is first-match on an ordered list — pricing complaints that mention "api" stay in `pricing`, not `integration`.

#### Known classifier weaknesses
1. **Sarcasm:** VADER reads positive words as positive ("great, another outage" → positive)
2. **Neutral/negative boundary:** mildly frustrated short posts often score near 0
3. **Category overlap:** a slow-API complaint could be `reliability` or `integration`
4. **No-text items:** link-only stories give VADER a single-word title — weak signal

### Evaluation (Step 6)
- `scripts/sample_for_eval.py` draws a **stratified** sample (equal topic representation) of 25 records into `outputs/eval_sample.xlsx`.
- Fill in `manual_sentiment` and `manual_category` columns, then run `scripts/evaluate.py` for per-class precision/recall/F1 and a confusion matrix.
- See `outputs/eval_sample.xlsx` for the worksheet.

---

## Configuration

All tunable knobs are in `config.yaml` — nothing is hardcoded in Python:

```yaml
topics:
  - AI coding agents
  - Startup Culture
  - Self-driving cars
  - Four-day work week
  - Return to office mandates

crawler:
  hits_per_page: 50
  pages_per_topic: 2
  max_total_items: 500
  sleep_seconds: 1.0

paths:
  db: data/hn_pipeline.db
  raw_dir: data/raw
  outputs_dir: outputs

classifier:
  sentiment_positive_threshold: 0.05
  sentiment_negative_threshold: -0.05

summary:
  top_items_per_topic: 3

evaluation:
  sample_size: 25
```

---

## Time spent

| Phase | Time |
|---|---|
| Design + folder structure | ~15 min |
| Step 1–3 (schema, crawler, transform) | ~45 min |
| Step 4 (wiring + idempotency proof) | ~20 min |
| Step 5 (classifier) | ~20 min |
| Step 6 (evaluation scripts) | ~15 min |
| Step 7 (summariser) | ~15 min |
| Step 8 (README) | ~20 min |
| Debugging (API tags parameter) | ~10 min |
| **Total** | **~2h 40min** |

---

## Classifier Evaluation Results

*(This section is filled in after hand-labelling `outputs/eval_sample.xlsx` and running `python scripts/evaluate.py`.)*

To reproduce:
```bash
# 1. Open outputs/eval_sample.xlsx
# 2. Fill in manual_sentiment (positive/negative/neutral)
#    and manual_category for each row
# 3. Save and run:
python scripts/evaluate.py
```

---

## Development Tools

This was built and refined with the assistance of:
- **Antigravity**
- **Claude Model**
