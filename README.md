# SPX Discord alerts

One private service: **bullish, bearish and volatile SPX observations from completed candles**. It tells you what changed and why. It does not select options, execute orders or claim a validated trading edge.

## What is present—and what enables it

| Feature | Status / enable mechanism | Evidence |
|---|---|---|
| SPX minute collection | Live; `COLLECTION_ENABLED=true` + Theta key | First-seen bars, receipt times, request counters |
| Bullish/bearish state | Live; fixed `spx-state-v1` rules | Completed 5m observations using completed 15m context |
| Volatile state | Live; independent of direction, same rule version | Relative true range / BB-width evidence |
| Private Discord alerts | Live; collection + `PUBLICATION_ENABLED=true` + private `SPX_CHANNEL_ID` | Durable transition/delivery ledger |
| Cached `!status` (`!bb` alias), `!help` | Installed even with collection off; status restricted to configured channel | No provider call and no new alert |
| Session review / feedback CSV | Manual; `python -m market_state.review DATE` | Recorded observations and operational counts; not performance grading |
| Bounded request telemetry | Optional original `THETA_AUDIT_START_DATE` / `THETA_AUDIT_DAYS` window | Local request metadata, purpose `market_state` |
| Historical research/readers | Manual, separate `research/` package and dependencies | Old data and experiments, never scheduled by the bot |

**Removed, not merely disabled:** old forecast/swing/flow/context/BB-event notification cogs and UI, ORB background experiment, economic notices, auto-tuning, automatic Git commit/pull/push, old Discord dashboards/commands and live legacy settings. No notification-mode selector, background research scheduler, AI service or provider fallback exists. Historical data and selected readers remain; [research inventory](research/README.md).

## Alert behavior

- **5m decides the state; 15m supplies completed directional context; 1m is display context only.** BB(20,2), RSI(14), MACD(12,26,9).
- Bullish/bearish requires two consecutive qualifying current-session 5m candles. Exact symmetric thresholds are in the [rule contract](docs/market-state.md).
- Volatility is measured separately. **BULLISH + VOLATILE**, **BEARISH + VOLATILE**, or **VOLATILE — no directional bias** are valid alerts. Conflicting indicators are not automatically volatility.
- Notify on the first directional/volatile state and subsequent direction/volatility changes. Initial neutral is silent; returning to neutral clears a prior bias once. Unchanged candles do not repeatedly notify.
- Stale, missing or warming-up data is unavailable, never a fabricated neutral or directional signal.
- Two preceding sessions warm the indicators. With sufficient valid history, the earliest classification is **09:50 ET**. Holidays, short sessions or missing history can delay readiness.
- Regular-session alerts only. A decision expires 90 seconds after its 5m close; a new minute cannot extend it. No post-close alerts, including the final candle when its response arrives after the close.
- Observations—not last successful deliveries—drive transitions. Enabling publication later does not replay suppressed states. Ambiguous sends are not automatically retried.

These are explicit implementation conventions, **not optimized or proven SPX rules**. Completed candles prevent using unfinished prices; later-arriving history can still change a recomputation. Status explicitly identifies a same-candle disagreement with the recorded observation.

## Setup

Use **Python 3.12+** for the official Theta SDK. The local offline suite can run without contacting that SDK; it is not a substitute for validating the Pi environment.

```bash
python -m pip install -r requirements.txt
# On a NEW installation only:
cp .env.example .env
# Edit .env privately, then:
python main.py
```

On an existing installation, **edit the existing `.env`; do not overwrite credentials or reset audit dates**.

To collect and send alerts, explicitly set:

```env
COLLECTION_ENABLED=true
PUBLICATION_ENABLED=true
SPX_CHANNEL_ID=YOUR_PRIVATE_CHANNEL_ID
```

Also configure `DISCORD_TOKEN`, `THETADATA_API_KEY`, Discord Message Content Intent and permission to view/send messages and embed links in the owner-only channel. Verify who can read the channel and the applicable data licence; a configured ID alone does not verify privacy or redistribution rights.

Both switches default **off**, including upgrades. Collection off means no provider requests. Publication off means no alerts, but explicitly enabled collection continues. Missing credentials never activate a fallback. Polling defaults to 60 seconds and accepts 30–60. Old `SPX_CONTEXT_CHANNEL_ID` / `SPX_CONTEXT_POLL_SECONDS` are migration aliases only when their new names are absent; all other old feature settings are ignored.

No subscription is changed by this software. Active collection requests index data only; retained manual research can request options data. Verify account-specific entitlements before downgrading. Audit expiry stops request metadata—not collection or vendor billing.

## Where state lives

- `.thetadata-data/market-state/state-YYYY-MM-DD.json`: neutral bars, warm-up evidence, immutable assessed states, delivery attempts and source lineage.
- `.thetadata-data/audit/usage-YYYY-MM-DD.jsonl`: request metadata within the original audit window.
- `.thetadata-data/bb-events/`, `.thetadata-data/context/`, old audit records and `data/`: retained historical evidence, not active strategies.
- `logs/bot.log*`: operational logs. No runtime Git backup or synchronization.

Files remain private and ignored by Git. Establish a licence-compliant private backup separately. On first load, validated same-day BB evidence is preferred; otherwise legacy context/ORB evidence supplies bars only. Source files remain untouched and migration records their hashes. ORB is never run or graded. Corrupt/conflicting migration evidence stops collection before requests or replacement writes; reconcile explicitly, never delete history to bypass the check.

One OS-backed lock protects the evidence directory. A second normal service instance refuses startup. **Do not delete `.service.lock`**: the file can remain after shutdown; the OS handle determines ownership. Shutdown retains ownership until outstanding provider work finishes. This is a single-machine service, not a distributed deployment.

## Review without collecting

```bash
python -m market_state.review 2026-09-08
python -m market_state.review 2026-09-08 --csv private-review.csv
```

Reads an existing ledger only—no migration, provider request or source write. CSV includes every recorded observation, even unchanged/silent states, with blank usefulness/notes columns; existing output files are never overwritten. Keep exports private. Counts cover **recorded fresh classifications**, not every possible session opportunity. Direction and volatility are counted independently. No win rate, option P&L or predictive outcome evaluation is implemented.

## Validate

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests --ignore=tests/historical
python -m compileall -q bot core market_state
python -c "import main, bot.core, market_state.cog"
# Optional historical-reader/research validation:
python -m pip install -r requirements-research.txt
python -m pytest -q
```

Latest local validation: **355 tests passed, plus 8 subtests**, on Python 3.12.3 with Theta SDK 1.0.10 installed (also passed on Python 3.11). The live/shared subset has 227 tests. SDK imports were checked without provider requests. Windows process locking was exercised; Unix locking and the Pi lifecycle remain deployment checks.

Tests cover the state rules, causality, freshness, migration, disk failures, deduplication, process ownership, disabled controls, real cog assembly and offline review. Removed feature tests were retired rather than restoring old code to preserve a test count. Live provider/Discord behavior, indicator parity and predictive usefulness still require separate evidence.

## Existing Pi deployment

Only after these changes have been reviewed and merged to remote `master`:

1. Stop the existing bot with its current `make stop`; verify the old process has exited before starting this version. The retired bot does not participate in the new evidence lock.
2. Pull `master` in the existing clone; preserve `.env` and licensed evidence. Update any external research commands to `python -m research.<tool>`.
3. On Python 3.12+, install updated requirements. Set the two explicit opt-ins and verified private destination when ready.
4. Run `make restart`, `make status`, `make tail`. `make status` checks process existence only; inspect `!status` for data age, readiness and effective collection/publication.
5. During market hours check complete candles, first warm-up, one genuine state transition, restart deduplication, and collection-off/publication-off independently. Do not test by fabricating a live trade recommendation.

No systemd/Docker migration, remote merge, subscription change or Pi deployment is implied by a local code change.

[Exact rule/delivery contract](docs/market-state.md) · [Scope decision](docs/product-scope.md) · [Research tools](research/README.md)
