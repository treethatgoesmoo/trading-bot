import os
from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "1") -> bool:
    """Boolean env var: anything but 0/false/empty is on."""
    return os.getenv(name, default) not in ("0", "false", "False", "")


# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))

# Feature toggles
ZERO_DTE_ENABLED = _flag("ZERO_DTE_ENABLED")
SWING_ENABLED = _flag("SWING_ENABLED")
# Isolated, paper-only SPXW turning-point shadow watch. Disabled by default
# because its provisional rules have not established predictive edge.
SPX_FLOW_ENABLED = _flag("SPX_FLOW_ENABLED", "0")

# API keys (Twelve Data and Finnhub are primary; yfinance is the free fallback)
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
# ThetaData v3 direct Python client (Python 3.12+; no local Java terminal).
# Prefer a revocable API key over storing the account password.
THETADATA_API_KEY = os.getenv("THETADATA_API_KEY", "")

# Rate limits
TWELVEDATA_CALLS_PER_MIN = 7
TWELVEDATA_DAILY_LIMIT = 750
FINNHUB_CALLS_PER_MIN = 55

# Trading
TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "SPY").split(",") if t.strip()]
# SMA_PERIOD/RISK_REWARD_RATIO are no longer read by the live bot (morning
# bias + break-and-retest entries were removed) but stay live for
# backfill.py's historical bias replay and backtest_filters.py's audit of
# recorded break-and-retest history — see docs/state-of-the-code.md.
SMA_PERIOD = 50
RISK_REWARD_RATIO = 2.0

# Indicator filter settings (RVOL is reused by the volume-spike watcher below;
# EMA/ATR-overextension/VWAP-filter settings tied to the removed
# break-and-retest entry filters are gone — see docs/state-of-the-code.md)
RVOL_THRESHOLD = 1.2
RVOL_LOOKBACK = 10

# Intraday directional forecast cadence (minutes). Also the candle interval
# the forecast is computed on and graded against.
FORECAST_INTERVAL_MINUTES = int(os.getenv("FORECAST_INTERVAL_MINUTES", "15"))
# Contradiction guard: when the losing side carries at least this fraction of
# the winning side's weight, the forecast is VOLATILE (yellow caution, with a
# lean) instead of a direction. Weight ratio only — no vote-count minimum: a
# lone dissenting heavyweight is the worst case (graded 0/6 on live history),
# while multi-vote/low-weight splits below this ratio graded 60%+. Calibrated
# on results-*.json rescored with the current tuned weights: ratio >= 0.75
# graded 27% vs 61% directional.
FORECAST_CONFLICT_RATIO = float(os.getenv("FORECAST_CONFLICT_RATIO", "0.75"))
# Low-confidence gate: a directional forecast below this confidence is posted
# as VOLATILE (winning side kept as the lean) instead of a direction. At the
# default 65 this subsumes the conflict-ratio guard (ratio >= 0.75 caps
# confidence at ~57%), which stays as a backstop if this gate is lowered.
FORECAST_VOLATILE_CONFIDENCE = float(os.getenv("FORECAST_VOLATILE_CONFIDENCE", "65"))
# Volatility-scaled grading deadband (triple-barrier-style flat class): a
# window whose |move| is under this fraction of the interval ATR(14) grades
# as FLAT — directional calls on it go ungraded (noise, not a miss) and
# NEUTRAL/VOLATILE calls on it grade correct. Median 15m SPY window moved
# ~$0.47 while graded accuracy sat at coin-flip; forcing those tiny moves
# into BULLISH/BEARISH labels fed the tuner noise (see arXiv 2303.04581 on
# fixed-threshold labeling). Also reused by the volume-spike watcher's grading.
FORECAST_DEADBAND_ATR_MULT = float(os.getenv("FORECAST_DEADBAND_ATR_MULT", "0.25"))
# Posting mode: "change" posts a forecast only when the read changed —
# direction flip, internal directional-strength change, range-state change, or heartbeat —
# while every computed forecast is still logged and graded. "always" restores
# the old post-every-cycle firehose.
FORECAST_POST_MODE = os.getenv("FORECAST_POST_MODE", "change").lower()
FORECAST_REPOST_CONFIDENCE_DELTA = float(os.getenv("FORECAST_REPOST_CONFIDENCE_DELTA", "15"))
FORECAST_HEARTBEAT_MINUTES = int(os.getenv("FORECAST_HEARTBEAT_MINUTES", "60"))
FORECAST_START_HOUR = 10
FORECAST_START_MINUTE = 0
FORECAST_END_HOUR = 16
FORECAST_END_MINUTE = 0

# (DAY_FORECAST_*, SPX_OPTIONS_* knobs removed 2026-08-12 with their
# features — the actual tracked session high/low replaced both. Morning
# bias, break-and-retest entries, the 5m companion forecast stream, the
# standalone VIX spike/regime watcher, and the standalone candlestick
# pattern watcher were removed in a later pass — see
# docs/state-of-the-code.md for the full list and rationale.)

# Post the latest git change to the alert channel when the bot (re)starts.
STARTUP_CHANGELOG_ENABLED = _flag("STARTUP_CHANGELOG_ENABLED")

# Market hours (Eastern Time) — still read by bot/shared_cog.py and
# swing/cog.py even though zero_dte's own premarket/first-candle logic
# (which used to read this too) was removed.
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30

# Economic events (Kalshi public market-data API — no key required)
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
ECON_EVENTS_ENABLED = _flag("ECON_EVENTS_ENABLED")
ECON_LOOKAHEAD_DAYS = int(os.getenv("ECON_LOOKAHEAD_DAYS", "14"))  # Briefing look-ahead window

# RSI thresholds used by the intraday forecast and retained historical
# divergence-research settings. Divergence is no longer a live voter.
RSI_OB_WARN = 70.0     # overbought warning
RSI_OS_WARN = 30.0     # oversold warning
RSI_DIV_LOOKBACK = 40  # bars to scan for divergence pivots
RSI_PIVOT_ORDER = 2    # swing strength (bars on each side of a pivot)
RSI_DIV_RSI_GATE = 50.0  # divergence counts only while RSI is still on the reversal side
WATCH_INTERVAL_SECONDS = 60  # min seconds between BB/volume watcher runs

# Experimental SPX/SPXW flow watch. Raw OPRA records stay in memory; compact
# shadow observations are written only under ignored .thetadata-data/.
SPX_FLOW_ALERT_CHANNEL_ID = int(os.getenv("SPX_FLOW_ALERT_CHANNEL_ID", "0")) or ALERT_CHANNEL_ID
SPX_FLOW_POLL_SECONDS = max(30, int(os.getenv("SPX_FLOW_POLL_SECONDS", "60")))
SPX_FLOW_LOOKBACK_MINUTES = max(1, int(os.getenv("SPX_FLOW_LOOKBACK_MINUTES", "5")))
SPX_FLOW_STRIKE_RANGE = max(0, int(os.getenv("SPX_FLOW_STRIKE_RANGE", "2")))
SPX_FLOW_ALERT_COOLDOWN_MINUTES = max(
    0, int(os.getenv("SPX_FLOW_ALERT_COOLDOWN_MINUTES", "10"))
)

# Bollinger band touch watcher — pings when a closed 1m/5m candle closes at
# or beyond the upper/lower band. Fires once per touch (re-arms only after a
# close back inside the band) with a per-band cooldown as a backstop.
# Fetches 1m bars from yfinance directly — zero Twelve Data/Finnhub budget.
BB_WATCH_ENABLED = _flag("BB_WATCH_ENABLED")
BB_TICKERS = [t.strip().upper() for t in os.getenv("BB_TICKERS", "SPY").split(",") if t.strip()]
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_STD = float(os.getenv("BB_STD", "2.0"))
BB_ALERT_COOLDOWN_MINUTES = int(os.getenv("BB_ALERT_COOLDOWN_MINUTES", "15"))

# Volume-spike watcher — pings when a completed 5m candle's volume clears
# VOLUME_SPIKE_MULT x its trailing VOLUME_SPIKE_LOOKBACK-bar average. Fires
# once per spike (re-arms once volume drops back under the threshold), with
# a per-ticker cooldown as a backstop — mirrors the BB watcher's shape.
# Fetches 1m bars from yfinance directly (resampled to 5m) — zero Twelve
# Data/Finnhub budget.
VOLUME_WATCH_ENABLED = _flag("VOLUME_WATCH_ENABLED")
VOLUME_WATCH_TICKERS = [t.strip().upper() for t in os.getenv("VOLUME_WATCH_TICKERS", "SPY").split(",") if t.strip()]
VOLUME_SPIKE_LOOKBACK = int(os.getenv("VOLUME_SPIKE_LOOKBACK", "20"))
VOLUME_SPIKE_MULT = float(os.getenv("VOLUME_SPIKE_MULT", "2.0"))
VOLUME_ALERT_COOLDOWN_MINUTES = int(os.getenv("VOLUME_ALERT_COOLDOWN_MINUTES", "15"))

# VIX ("fear gauge", inverse to SPY) — still folded into the 15m forecast
# ensemble as a weighted vote (core/vix.py::forecast_signal). The standalone
# VIX spike/regime watcher and its !vix command were removed.
VIX_SIGNAL_DEADBAND_PCT = 0.3  # early-day fallback: VIX 30-min move below this % is neutral
VIX_SIGNAL_MIN_Z = float(os.getenv("VIX_SIGNAL_MIN_Z", "1.0"))  # 30-min move must clear this many std devs of today's moves to vote
VIX_FORECAST_WEIGHT = 2.0     # weight of the VIX vote in the 15-min forecast

# Nightly self-tuning: retune forecast + swing indicator weights from graded
# history after each session close (before the git auto-commit).
AUTO_TUNE_ENABLED = _flag("AUTO_TUNE_ENABLED")
# Rolling window for the nightly 0DTE retune (days of graded history; 0 = all).
# Keeps pre-deadband force-labeled days from dominating the stats forever.
AUTO_TUNE_DAYS = int(os.getenv("AUTO_TUNE_DAYS", "30"))
# Tuned weights never go below this. A 0-weight indicator still "votes" but
# vanishes from the confidence denominator and the VOLATILE conflict guard —
# one zeroed dissenter let a 4-indicator forecast print 100% confidence.
TUNED_WEIGHT_FLOOR = float(os.getenv("TUNED_WEIGHT_FLOOR", "0.3"))
# Shrinkage prior for the accuracy -> weight mapping: measured accuracy is
# pulled toward 50% as if this many coin-flip pseudo-samples were mixed in,
# so a 60% read off 30 bars moves weight far less than 60% off 300 bars.
TUNED_WEIGHT_SHRINKAGE = float(os.getenv("TUNED_WEIGHT_SHRINKAGE", "50"))
# Max weight movement per retune (per indicator). The nightly loop re-runs
# daily, so a real edge still gets there in a week — noise doesn't.
TUNED_WEIGHT_MAX_STEP = float(os.getenv("TUNED_WEIGHT_MAX_STEP", "0.5"))

# Auto-commit captured history (data/) to git after market close
GIT_AUTOCOMMIT_ENABLED = _flag("GIT_AUTOCOMMIT_ENABLED")
GIT_AUTOCOMMIT_PUSH = _flag("GIT_AUTOCOMMIT_PUSH")
GIT_AUTOCOMMIT_DELAY_MINUTES = 10  # commit this long after session close (lets EOD eval finish)

# ── Swing options feature ────────────────────────────────────────────────
# Daily-timeframe signals on popular names, suggesting 1-2 week option buys.
SWING_TICKERS = [t.strip().upper() for t in os.getenv("SWING_TICKERS", "TSLA,AMD,GOOG,META").split(",") if t.strip()]
SWING_ALERT_CHANNEL_ID = int(os.getenv("SWING_ALERT_CHANNEL_ID", "0")) or ALERT_CHANNEL_ID

# Scan schedule (ET): pre-open scan + midday confirmation check
SWING_PREOPEN_OFFSET_MINUTES = 30   # scan this long before market open (9:00 ET)
SWING_MIDDAY_HOUR = 12
SWING_MIDDAY_MINUTE = 30

# Signal thresholds
SWING_SCORE_THRESHOLD = 4.0     # min |net weighted score| to fire
SWING_MIN_CONFIDENCE = 65.0     # min confidence (%) to fire
SWING_COOLDOWN_DAYS = 3         # no re-signal per ticker+direction while open/recent

# Contract selection
SWING_DTE_MIN = 7
SWING_DTE_MAX = 14
SWING_DTE_FALLBACK_MAX = 21     # widen to this when no expiry in the primary window
SWING_STRIKE_OTM_MAX_PCT = 3.0  # consider strikes from ATM to this % OTM
SWING_MIN_OPEN_INTEREST = 100
SWING_MAX_SPREAD_PCT = 10.0     # wider bid-ask than this -> illiquid warning

# Grading
SWING_TARGET_MOVE_PCT = 3.0     # underlying move that counts as a win
SWING_HORIZON_CAL_DAYS = 14     # grade at min(expiry, signal + this many days)
SWING_ATR_STOP_MULT = 1.5       # invalidation = close -/+ this x daily ATR(14)

# Entry timing (signals fire pre-open off yesterday's close; buying the open
# blind is the worst fill of the day). Zone depth: a dip of this x ATR(14)
# below the signal close is a normal pullback, not a broken thesis — validate
# with `python backtest_swing.py` (adverse-excursion section). Chase cap: a
# run of this x ATR past the signal close voids the quoted premium/breakeven.
SWING_ENTRY_ZONE_ATR_MULT = float(os.getenv("SWING_ENTRY_ZONE_ATR_MULT", "0.5"))
SWING_ENTRY_CHASE_ATR_MULT = float(os.getenv("SWING_ENTRY_CHASE_ATR_MULT", "0.5"))
SWING_ENTRY_AVOID_OPEN_MINUTES = int(os.getenv("SWING_ENTRY_AVOID_OPEN_MINUTES", "15"))

# Polling
POLL_INTERVAL_SECONDS = 60  # How often to check for setups

# Bot command prefix
COMMAND_PREFIX = "!"
