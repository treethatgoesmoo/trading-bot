"""Single source of truth for filesystem paths.

Modules must never derive paths from their own __file__ — that breaks silently
when code moves between packages. Everything anchors to the repo root here.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")
# Licensed ThetaData/OPRA snapshots are local-only and must never enter data/.
THETADATA_DATA_DIR = os.path.join(ROOT, ".thetadata-data")

# Historical research paths; no live watchlist or swing state remains.
API_BUDGET_FILE = os.path.join(ROOT, ".api_budget.json")

INDICATOR_WEIGHTS_FILE = os.path.join(DATA_DIR, "indicator_weights.json")
SWING_WEIGHTS_FILE = os.path.join(DATA_DIR, "swing_weights.json")
DASHBOARD_OUT = os.path.join(ROOT, "dashboard.html")
