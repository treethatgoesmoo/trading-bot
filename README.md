# Trading Bot

Personal Discord decision-support bot for manual trading. It posts and grades market observations; it **never places orders**.

## Current features

| Feature | Default | Purpose |
|---|---:|---|
| SPY 15-minute forecast | On | Change-driven directional/volatile forecast with backend grading |
| Bollinger-touch watcher | On | Descriptive five-minute extension warning |
| Volume-spike watcher | On | Descriptive five-minute relative-volume warning |
| Swing signals | On | Daily signals for TSLA, AMD, GOOG, and META with paper 7–14 DTE contract suggestions |
| SPXW flow shadow | Off | Experimental, paper-only turning-point and continuation observations using ThetaData |

The forecast also displays completed 30-minute and one-hour candle direction, momentum, and stretch as **context only**. These fields are not independent trade signals. See [docs/state-of-the-code.md](docs/state-of-the-code.md) for the authoritative current snapshot, [docs/next-steps.md](docs/next-steps.md) for the prioritized roadmap, and [docs/strategy-log.md](docs/strategy-log.md) for the evidence history.

## Setup

Python 3.12 or newer is required by the ThetaData v3 dependency.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Minimum `.env` configuration:

```env
DISCORD_TOKEN=...
ALERT_CHANNEL_ID=...
```

Twelve Data and Finnhub keys are optional; regular OHLCV falls back to yfinance. The experimental SPXW shadow additionally requires:

```env
THETADATA_API_KEY=...
SPX_FLOW_ENABLED=1
```

Keep real credentials only in the ignored `.env` file.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
python -c "import main, bot.core, zero_dte.cog, zero_dte.flow_cog, swing.cog, core.analytics"
```

Run the relevant backtest before changing a strategy. Reproducible research commands are documented in `CLAUDE.md` and the records under `docs/research/`.

## Data and safety boundaries

- `data/` is committed bot history and may be auto-committed after the close. Do not hand-edit or delete it.
- `.research-data/` holds ignored frozen Yahoo research inputs.
- `.thetadata-data/` holds ignored licensed ThetaData/OPRA snapshots and paper-shadow outcomes. Never commit or redistribute raw OPRA data.
- Stored timestamps are UTC ISO; user-facing times are US/Eastern.
- The SPXW feature is experimental, defaults off, cannot execute trades, and does not affect the established SPY forecast.
