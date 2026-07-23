.PHONY: run run-again sample-eval evaluate clean help

# ─────────────────────────────────────────────────────────────────────────────
# Main targets
# ─────────────────────────────────────────────────────────────────────────────

## run: Run the full pipeline (crawl → transform → classify → summarise)
run:
	python -m pipeline --config config.yaml

## run-again: Run a second time — proves idempotency (row counts must not change)
run-again:
	@echo "Running pipeline a second time to verify idempotency..."
	python -m pipeline --config config.yaml

## sample-eval: Sample records into a CSV for hand-labelling (Step 6)
sample-eval:
	python scripts/sample_for_eval.py --config config.yaml

## evaluate: Compare hand labels to classifier output (run AFTER filling the xlsx)
evaluate:
	python scripts/evaluate.py --file outputs/eval_sample.xlsx

## install: Install Python dependencies
install:
	pip install -r requirements.txt

## clean: Remove generated data (keeps raw cache to avoid re-fetching)
clean:
	rm -f data/hn_pipeline.db
	rm -f outputs/summary.md
	rm -f outputs/eval_sample.csv
	rm -f outputs/eval_sample.xlsx

## clean-all: Remove everything including raw cache (will re-fetch on next run)
clean-all: clean
	rm -rf data/raw/*

## help: Show this help message
help:
	@grep -E '^##' Makefile | sed 's/## /  /'
