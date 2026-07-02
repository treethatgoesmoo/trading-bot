"""Generic per-day JSONL/JSON persistence under data/.

Kind-agnostic helpers shared by every feature's tracker. File naming
convention: data/{kind}-YYYY-MM-DD.jsonl (append-only event logs) or .json
(rewritten aggregates). Writes that replace a file go through write_atomic so
a crash mid-write can't corrupt captured history.
"""

from __future__ import annotations

import json
import os

from core.paths import DATA_DIR


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
    """Load a per-day JSONL file for a kind (forecasts, bias, swing_signals, ...)."""
    path = os.path.join(DATA_DIR, f"{kind}-{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


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
