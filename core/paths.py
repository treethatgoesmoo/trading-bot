"""Single source of truth for filesystem paths.

Modules must never derive paths from their own __file__ — that breaks silently
when code moves between packages. Everything anchors to the repo root here.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOGS_DIR = os.path.join(ROOT, "logs")
# Market data is local-only. Keep the retired feed's ledgers separate.
MARKET_DATA_DIR = os.path.join(ROOT, ".alpaca-data")
LEGACY_MARKET_DATA_DIR = os.path.join(ROOT, ".thetadata-data")
