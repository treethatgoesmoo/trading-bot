"""Settings for explicitly invoked historical research only.

Nothing in the live bot imports this module. Defaults preserve the retired
forecast/swing replay conventions, not the current SPX market-state rules.
No Discord destinations, notification flags, schedules or Git controls belong
here. Provider credentials are used only by explicit research invocations.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from core.paths import ROOT

load_dotenv(Path(ROOT) / '.env')

# Optional historical data providers; not live fallbacks.
TWELVEDATA_API_KEY = os.getenv('TWELVEDATA_API_KEY', '')
FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', '')
TWELVEDATA_CALLS_PER_MIN = 7
TWELVEDATA_DAILY_LIMIT = 750
FINNHUB_CALLS_PER_MIN = 55

# Frozen historical forecast/replay conventions.
TICKERS = [t.strip().upper() for t in os.getenv('TICKERS', 'SPY').split(',') if t.strip()]
SMA_PERIOD = 50
RISK_REWARD_RATIO = 2.0
RVOL_THRESHOLD = 1.2
RVOL_LOOKBACK = 10
FORECAST_INTERVAL_MINUTES = int(os.getenv('FORECAST_INTERVAL_MINUTES', '15'))
FORECAST_CONFLICT_RATIO = float(os.getenv('FORECAST_CONFLICT_RATIO', '0.75'))
FORECAST_VOLATILE_CONFIDENCE = float(os.getenv('FORECAST_VOLATILE_CONFIDENCE', '65'))
FORECAST_DEADBAND_ATR_MULT = float(os.getenv('FORECAST_DEADBAND_ATR_MULT', '0.25'))
RSI_OB_WARN = 70.0
RSI_OS_WARN = 30.0
RSI_DIV_LOOKBACK = 40
RSI_PIVOT_ORDER = 2
VIX_SIGNAL_DEADBAND_PCT = 0.3
VIX_SIGNAL_MIN_Z = float(os.getenv('VIX_SIGNAL_MIN_Z', '1.0'))
VIX_FORECAST_WEIGHT = 2.0

# Explicit manual tuning, never an automatic production job.
TUNED_WEIGHT_FLOOR = float(os.getenv('TUNED_WEIGHT_FLOOR', '0.3'))
TUNED_WEIGHT_SHRINKAGE = float(os.getenv('TUNED_WEIGHT_SHRINKAGE', '50'))
TUNED_WEIGHT_MAX_STEP = float(os.getenv('TUNED_WEIGHT_MAX_STEP', '0.5'))

# Historical swing replay; no contracts, tracking cog or notification stream.
SWING_TICKERS = [t.strip().upper() for t in os.getenv('SWING_TICKERS', 'TSLA,AMD,GOOG,META').split(',') if t.strip()]
SWING_SCORE_THRESHOLD = 4.0
SWING_MIN_CONFIDENCE = 65.0
SWING_COOLDOWN_DAYS = 3
SWING_TARGET_MOVE_PCT = 3.0
SWING_HORIZON_CAL_DAYS = 14
SWING_ATR_STOP_MULT = 1.5
SWING_ENTRY_ZONE_ATR_MULT = float(os.getenv('SWING_ENTRY_ZONE_ATR_MULT', '0.5'))
