---
description: Retune indicator weights (zero_dte and/or swing) and report before/after deltas
argument-hint: "[zero_dte|swing|both] [--write]"
---
<!--
  Prompt template for weight retuning.
  pi users: trigger as /tune [zero_dte|swing|both] [--write]  ($@ = args)
  Other toolkits: paste this file into chat; replace $@ with your target + flags.
-->

Args: $@

## Step 1 — Snapshot current weights

Before retuning, read and record the current values:

- `data/indicator_weights.json` (zero_dte)
- `data/swing_weights.json` (swing)

## Step 2 — Run the retune

Based on the target in `$@` (default: both if unspecified):

```bash
python tune_weights.py --days 20 --write        # zero_dte
python tune_swing_weights.py --write            # swing
```

Omit `--write` if the user only wants a dry-run preview (default to dry-run unless `--write` was explicitly passed in `$@` or the user confirms).

## Step 3 — Report deltas

For each weight file touched, show a before/after table per indicator: old weight → new weight → delta. Call out:

- Any indicator whose weight flipped sign or changed by a large margin (possible overfit or a data quality issue worth investigating)
- Whether the retune was based on enough graded outcomes to be meaningful (thin sample sizes should be flagged, not silently trusted)

## Step 4 — Live reload note

Remind the user: weight files are hot-reloaded live by the running bot on file mtime change — no restart needed. If `--write` was used and the bot is running (`make status`), the new weights take effect within the next monitor loop tick.

## Step 5 — Follow-up

Suggest running the corresponding backtest (`/backtest`) to sanity-check the new weights before letting them run live for real trading days.
