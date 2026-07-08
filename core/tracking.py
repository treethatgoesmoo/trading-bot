"""Generic per-day JSONL/JSON persistence under data/.

Kind-agnostic helpers shared by every feature's tracker. File naming
convention: data/{kind}-YYYY-MM-DD.jsonl (append-only event logs) or .json
(rewritten aggregates). Writes that replace a file go through write_atomic so
a crash mid-write can't corrupt captured history.
"""

from __future__ import annotations

import json
import logging
import os

from core.paths import DATA_DIR

logger = logging.getLogger(__name__)


def write_atomic(path: str, text: str) -> None:
    """Write via temp file + rename so a crash mid-write can't corrupt history."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def append_jsonl(kind: str, date_str: str, entry: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{kind}-{date_str}.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_jsonl(kind: str, date_str: str) -> list[dict]:
    """Load a per-day JSONL file for a kind (forecasts, bias, swing_signals, ...).

    Skips corrupt lines (e.g. one truncated by a crash mid-append) instead of
    raising — one bad line must not take down !stats / grading for the day.
    """
    path = os.path.join(DATA_DIR, f"{kind}-{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("%s: skipping corrupt line %d", path, i)
    return entries


def load_json(path: str, default=None):
    """Best-effort load of a JSON file; returns default on missing/corrupt."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def available_dates(kind: str = "results") -> list[str]:
    """Sorted list of dates (YYYY-MM-DD) for which a given data file exists."""
    if not os.path.isdir(DATA_DIR):
        return []
    prefix = f"{kind}-"
    dates = []
    for fname in os.listdir(DATA_DIR):
        if fname.startswith(prefix) and (fname.endswith(".jsonl") or fname.endswith(".json")):
            stem = fname[len(prefix):].rsplit(".", 1)[0]
            if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
                dates.append(stem)
    return sorted(dates)
