"""Single source of truth for filesystem paths.

Modules must never derive paths from their own __file__ — that breaks silently
when code moves between packages. Everything anchors to the repo root here.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")

WATCHLIST_FILE = os.path.join(ROOT, ".watchlist.json")
SWING_WATCHLIST_FILE = os.path.join(ROOT, ".swing_watchlist.json")
SWING_STATE_FILE = os.path.join(ROOT, ".swing_daily_state.json")
API_BUDGET_FILE = os.path.join(ROOT, ".api_budget.json")

INDICATOR_WEIGHTS_FILE = os.path.join(DATA_DIR, "indicator_weights.json")
SWING_WEIGHTS_FILE = os.path.join(DATA_DIR, "swing_weights.json")
SWING_OPEN_FILE = os.path.join(DATA_DIR, "swing_open.json")

DASHBOARD_OUT = os.path.join(ROOT, "dashboard.html")
