# Trading Bot

Private Discord observations for manual SPX 0DTE trading. The bot **never places orders** or selects options. No predictive edge or option profitability has been established.

## Current notifications: Bollinger events + RSI/MACD

One stream watches completed **1m, 5m and 15m SPX candles**:

- Bollinger Band contact, close outside, rejection/back inside, and initial continuation.
- Each timeframe's BB levels, RSI and change, MACD histogram and change, and candle completion time.
- Event-only posts with persistent deduplication. No routine status heartbeat or automatic buy/sell recommendation.

The former SPX Action/Structure/Setup/Momentum/Confirmation/Invalidation indicator is removed from the running bot. Forecast, swing, old BB/volume, flow, economic, restart and retune notification paths are also removed. **There is no legacy-mode selector or fallback.** Historical modules and data remain for research and historical report compatibility; they are not installed as live cogs.

See [notification definitions and migration](docs/bb-notifications.md) and [research rationale](docs/research/bb-multitimeframe-and-ai-tools.md). These changes are local until committed, reviewed and merged; Pi deployment is not verified. Older SPX documentation describes the retired system, not this release.

## Setup / Pi upgrade

Python **3.12+** is required for the official ThetaData SDK. Install dependencies with `pip install -r requirements.txt`. For a fresh installation only, copy `.env.example` and supply credentials. **Never overwrite an existing Pi `.env`.**

Keep `DISCORD_TOKEN`, `THETADATA_API_KEY` and the existing `SPX_CONTEXT_CHANNEL_ID`. The channel must be private and comply with the market-data licence. Channel `0` collects locally but cannot publish; there is no fallback to an old alert channel. A missing Theta key starts no market stream.

```env
SPX_CONTEXT_CHANNEL_ID=<your-existing-private-channel-id>
SPX_CONTEXT_POLL_SECONDS=60
THETA_AUDIT_START_DATE=<original-fixed-start-date-or-blank>
THETA_AUDIT_DAYS=30
```

Do not reset the original audit start date when upgrading. Old `NOTIFICATION_MODE`, feature flags, proxy/flow, heartbeat and cooldown settings no longer control notifications and can be removed.

Once the changes reach remote `master`, use the existing Pi checkout:

```bash
git pull origin master
make restart
make status
make tail
```

Check fresh minute evidence and an actual BB event delivery during market hours. Two preceding trading sessions are fetched and cached for warm-up. With two normal complete sessions, 15m is ready at its first close, **09:45 ET**; missing or insufficient history is shown as warming up, not fabricated.

## Commands and audit

- `!bb` / `!status`: cached completed-candle indicators in the configured private channel; no provider requests. Stale snapshots are unavailable.
- `!audit`: current-session BB request/delivery counts and frozen ORB status. **BB outcomes are not yet evaluated**; this is not a win-rate report.
- `!cmd` / `!cmds`: installed command list.
- `!commit`: owner-only, private-channel tracked-history housekeeping.

The same index collector preserves the quiet frozen ORB experiment. It does not collect options flow or Yahoo SPY data. `python report_theta_audit.py --start YYYY-MM-DD --days 30` remains available for request telemetry and historical context/ORB reports; it does **not** grade new BB events. New flow-filtered comparisons have no new flow evidence.

Audit expiry stops telemetry/mirrors, **not live index collection or billing**. No paid AI service has been added.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot core zero_dte tests
python -c "import main, bot.core, zero_dte.bb_signal_cog, report_theta_audit"
```

Tests use synthetic data and mocked providers; they do not prove live data quality, Discord delivery, chart-platform parity or trading returns. Held-out event studies and paper evaluation are still required before treating these observations as trade signals.

## Data boundaries

- Never edit/delete committed `data/` history.
- Licensed BB snapshots, receipt times and delivery ledgers live under ignored `.thetadata-data/bb-events/`.
- `.thetadata-data/audit/` holds bounded mirrors and request metadata. Existing `.thetadata-data/context/` evidence is preserved unchanged.
- No licensed raw records belong in Git, public channels or third-party AI requests. The owner must verify channel permissions, licensing and private backup/retention.
- Stored timestamps are UTC; displayed candle times are ET.
