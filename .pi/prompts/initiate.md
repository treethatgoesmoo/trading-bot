---
description: Start a new piece of work — check env/git state, gather context, and clarify requirements before writing a single line of code
argument-hint: "<brief description of the work>"
---
<!--
  Prompt template for starting new work.
  pi users: trigger as /initiate <description>  ($@ = your typed description)
  Other toolkits: paste this file into chat; replace $@ with your work description.
-->

## Work Request

$@

---

## Phase 1 — Workspace Sanity Check

Run these and report results before continuing:

1. `git status --short` — flag any uncommitted changes; ask whether to stash/commit before starting if the tree is dirty.
2. `git log -1 --oneline` and current branch (`git branch --show-current`).
3. Confirm `.env` exists (`ls .env`) — if missing, warn that the bot/backtests needing live data won't run, but backtests using bulk yfinance data still work.
4. Quick smoke test (only if it hasn't run recently or structural files changed):

   ```bash
   python -c "import main, bot.core, zero_dte.cog, swing.cog, core.analytics"
   ```

If anything above fails, report it clearly and stop until resolved.

---

## Phase 2 — Understand the Work

Read the work request above. Figure out which area(s) it touches:

- **zero_dte/** — intraday First Candle Strategy, break-and-retest, forecasts, RSI/VIX watchers
- **swing/** — daily-timeframe weighted-vote option signals
- **core/** — shared infra (market_data, indicators, market_calendar, paths, tracking) — no Discord, no feature logic
- **bot/** — Discord shell, housekeeping loop, shared commands

Use `symbol_search` / `grep` / `module_report` to find the specific files, functions, and existing patterns relevant to the request — don't read whole files speculatively.

If the request proposes a new signal, filter, or strategy idea, **check `docs/strategy-log.md` first** — it records everything already tried, adopted, and rejected with numbers. Don't re-propose a rejected idea without new evidence.

Cross-check against CLAUDE.md's **Conventions & gotchas** (ET vs UTC timestamps, `paths.py` as the single path source, `data/` file-kind naming, `tasks.loop` error-handler convention, yfinance via `asyncio.to_thread`) for anything the request might collide with.

---

## Phase 3 — Ask Clarifying Questions

> Before asking in chat: check whether an interactive form tool (e.g. `interview`) is available. If so, use it for a structured UI with pre-selected recommendations. Otherwise ask in chat.

Ask **only** genuinely unresolved questions (2–5 max) — e.g. which feature is affected, whether a backtest should validate the change, whether existing `data/` file formats need to stay backward compatible, threshold/config values to use.

After the user answers, confirm shared understanding in 3–5 bullets before writing any plan or code.

---

## Phase 4 — Ready to Work

Once requirements are clear:

> "Environment checked, work area identified, requirements are understood. Shall I proceed with the implementation plan?"

Wait for the user's go-ahead. Remind them which backtest (if any — see `/backtest`) should validate the change once it's implemented.
