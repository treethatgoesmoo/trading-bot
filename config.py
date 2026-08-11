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

# API keys (Twelve Data and Finnhub are primary; yfinance is the free fallback)
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Rate limits
TWELVEDATA_CALLS_PER_MIN = 7
TWELVEDATA_DAILY_LIMIT = 750
FINNHUB_CALLS_PER_MIN = 55

# Trading
TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "SPY").split(",") if t.strip()]
SMA_PERIOD = 50
RISK_REWARD_RATIO = 2.0

# Indicator filter settings
RVOL_THRESHOLD = 1.2
RVOL_LOOKBACK = 10
ATR_PERIOD = 14
ATR_OVEREXTENDED_MULT = 3.0
EMA_FAST = 9
EMA_SLOW = 21

# Filter cooldown
FILTER_COOLDOWN_MINUTES = 5

# VWAP entry filter (long must enter above VWAP, short below). Off by default:
# it is the one filter in the stack whose vetoes were profitable. Over the
# logged setups every VWAP veto would have won — 7/10, avg +0.63R, +6.3R total
# blocked — while it was never the sole reason for a veto, so switching it off
# costs no extra risk. Every other filter's vetoes lost money (RVOL sole vetoes
# 0/9, -0.94R). See `python backtest_filters.py`. Kept as an advisory read
# rather than deleted: it is still computed, still shown, still logged, so the
# audit keeps scoring it on n>10 and this can be flipped back on the evidence.
VWAP_FILTER_ENABLED = _flag("VWAP_FILTER_ENABLED", "0")

# Trade tracking
TRADE_TRACKING_END_HOUR = 16
TRADE_TRACKING_END_MINUTE = 0

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
# Faster companion forecast on 5-min candles, posted every 5 minutes over the
# same window as the main forecast (graded against the next 5-min candle).
FORECAST_5M_ENABLED = _flag("FORECAST_5M_ENABLED")
FORECAST_5M_INTERVAL_MINUTES = 5
# Volatility-scaled grading deadband (triple-barrier-style flat class): a
# window whose |move| is under this fraction of the interval ATR(14) grades
# as FLAT — directional calls on it go ungraded (noise, not a miss) and
# NEUTRAL/VOLATILE calls on it grade correct. Median 15m SPY window moved
# ~$0.47 while graded accuracy sat at coin-flip; forcing those tiny moves
# into BULLISH/BEARISH labels fed the tuner noise (see arXiv 2303.04581 on
# fixed-threshold labeling).
FORECAST_DEADBAND_ATR_MULT = float(os.getenv("FORECAST_DEADBAND_ATR_MULT", "0.25"))
# Posting mode: "change" posts a forecast only when the read changed —
# direction flip, confidence jump, projected-range breach, or heartbeat —
# while every computed forecast is still logged and graded. "always" restores
# the old post-every-cycle firehose.
FORECAST_POST_MODE = os.getenv("FORECAST_POST_MODE", "change").lower()
FORECAST_REPOST_CONFIDENCE_DELTA = float(os.getenv("FORECAST_REPOST_CONFIDENCE_DELTA", "15"))
FORECAST_HEARTBEAT_MINUTES = int(os.getenv("FORECAST_HEARTBEAT_MINUTES", "60"))
FORECAST_START_HOUR = 10
FORECAST_START_MINUTE = 0
FORECAST_END_HOUR = 16
FORECAST_END_MINUTE = 0

# Morning day forecast: direction + ATR-projected high/low posted once the
# daily bias is set (~9:35 ET).
DAY_FORECAST_ENABLED = _flag("DAY_FORECAST_ENABLED")

# ── 0DTE SPX option range capture ────────────────────────────────────────
# Pick one same-day-expiry ATM call + put on the SPX chain once the opening
# range is set, sample their premiums through the session, and post the
# effective min/max at the close. The min is floored to settlement intrinsic
# — an OTM finish is worth 0 even though the quoted low never prints it.
# The initial projection follows the Opening Range Breakout (ORB) playbook:
# the first SPX_OPTIONS_ORB_MINUTES set the range, measured-move targets sit
# SPX_OPTIONS_ORB_TARGET_MULT x the range height beyond it, and each side's
# projected max is intrinsic at its breakout target.
SPX_OPTIONS_ENABLED = _flag("SPX_OPTIONS_ENABLED")
SPX_OPTIONS_TICKER = os.getenv("SPX_OPTIONS_TICKER", "^SPX").upper()
SPX_OPTIONS_ORB_MINUTES = int(os.getenv("SPX_OPTIONS_ORB_MINUTES", "15"))
SPX_OPTIONS_ORB_TARGET_MULT = float(os.getenv("SPX_OPTIONS_ORB_TARGET_MULT", "1.0"))
# Contracts are picked when the ORB window closes (9:45 ET by default).
SPX_OPTIONS_PICK_DELAY_MINUTES = int(os.getenv("SPX_OPTIONS_PICK_DELAY_MINUTES",
                                               str(SPX_OPTIONS_ORB_MINUTES)))
SPX_OPTIONS_SAMPLE_MINUTES = int(os.getenv("SPX_OPTIONS_SAMPLE_MINUTES", "5"))

# Post the latest git change to the alert channel when the bot (re)starts.
STARTUP_CHANGELOG_ENABLED = _flag("STARTUP_CHANGELOG_ENABLED")

# Market hours (Eastern Time)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
FIRST_CANDLE_READY_MINUTE = 35  # 9:35 AM ET — first 5-min candle is complete
MONITORING_END_HOUR = 11        # Stop monitoring at 11:00 AM ET
MONITORING_END_MINUTE = 0

# Economic events (Kalshi public market-data API — no key required)
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
ECON_EVENTS_ENABLED = _flag("ECON_EVENTS_ENABLED")
ECON_LOOKAHEAD_DAYS = int(os.getenv("ECON_LOOKAHEAD_DAYS", "14"))  # Briefing look-ahead window

# RSI reversal watcher (real-time alerts on 5m + 15m).
# Standalone pings default OFF: they duplicated/contradicted the intraday
# forecast posted alongside them — RSI reversal state now stamps a caution
# line on the forecast embed instead (see compute_30m_forecast.rsi_caution).
# `!rsi` still checks live on demand; set RSI_WATCH_ENABLED=1 to re-enable.
RSI_WATCH_ENABLED = _flag("RSI_WATCH_ENABLED", "0")
RSI_PERIOD = 14
RSI_OB_WARN = 70.0     # overbought warning
RSI_OB_EXTREME = 80.0  # overbought extreme
RSI_OS_WARN = 30.0     # oversold warning
RSI_OS_EXTREME = 20.0  # oversold extreme
RSI_DIV_LOOKBACK = 40  # bars to scan for divergence / failure-swing pivots
RSI_PIVOT_ORDER = 2    # swing strength (bars on each side of a pivot)
# Which divergence directions to alert on: both | bullish | bearish | off.
# Default off: standalone divergence pings contradicted the forecast posted
# moments earlier — divergence now votes inside the intraday forecast instead
# (compute_30m_forecast checks the forecast frame, then 5m).
RSI_DIVERGENCE_DIRECTIONS = os.getenv("RSI_DIVERGENCE_DIRECTIONS", "off").lower()
RSI_WATCH_INTERVAL_SECONDS = 60  # min seconds between watcher runs
# Anti-spam: identical (timeframe, kind, direction, stage) alerts are
# suppressed for this long — RSI oscillating around 30/70 otherwise pings on
# every crossing, and sliding-window pivots re-fire the same divergence.
RSI_ALERT_COOLDOWN_MINUTES = int(os.getenv("RSI_ALERT_COOLDOWN_MINUTES", "30"))
RSI_DIV_RSI_GATE = 50.0     # divergence pings only while RSI is still on the reversal side
RSI_FS_STALE_MARGIN = 10.0  # failure-swing break further than this past its trigger is stale

# Bollinger band touch watcher — pings when a closed 1m/5m candle closes at
# or beyond the upper/lower band. Fires once per touch (re-arms only after a
# close back inside the band) with a per-band cooldown as a backstop.
# Fetches 1m bars from yfinance directly — zero Twelve Data/Finnhub budget.
BB_WATCH_ENABLED = _flag("BB_WATCH_ENABLED")
BB_TICKERS = [t.strip().upper() for t in os.getenv("BB_TICKERS", "SPY").split(",") if t.strip()]
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_STD = float(os.getenv("BB_STD", "2.0"))
BB_ALERT_COOLDOWN_MINUTES = int(os.getenv("BB_ALERT_COOLDOWN_MINUTES", "15"))

# VIX ("fear gauge", inverse to SPY) — confirming signal + spike alerts
VIX_WATCH_ENABLED = _flag("VIX_WATCH_ENABLED")
VIX_ELEVATED = 20.0   # regime thresholds
VIX_HIGH = 25.0
VIX_FEAR = 30.0
VIX_LEVEL_HYSTERESIS = 0.3    # regime cross-back must clear the level by this (no 19.9↔20.2 ping-pong)
VIX_SPIKE_PCT = 5.0           # intraday % surge that triggers a spike alert
VIX_SPIKE_WINDOW_BARS = 3     # over this many 5-min bars (~15 min)
VIX_SPIKE_COOLDOWN_SEC = 900  # min seconds between spike alerts
VIX_SIGNAL_DEADBAND_PCT = 0.3  # early-day fallback: VIX 30-min move below this % is neutral
VIX_SIGNAL_MIN_Z = float(os.getenv("VIX_SIGNAL_MIN_Z", "1.0"))  # 30-min move must clear this many std devs of today's moves to vote
VIX_FORECAST_WEIGHT = 2.0     # weight of the VIX vote in the 30-min forecast

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
