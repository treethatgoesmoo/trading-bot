"""Opt-in, local-only request metadata; never persist request arguments or data.

Window is [THETA_AUDIT_START_DATE, start + THETA_AUDIT_DAYS), in Eastern
calendar dates. A request admitted in the window always gets its completion in
its start-date file, even if it completes after midnight or the window ends.
Unmatched starts mean pending/interrupted requests, not proven network hangs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
import time
import uuid

import config
from core.market_calendar import ET
from core.paths import THETADATA_DATA_DIR

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_ENDPOINTS = {"stock_history_ohlc": "stock"}
# Closed vocabulary prevents caller labels from leaking secrets into metadata.
PURPOSES = frozenset({"unspecified", "spy_setup"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def audit_active(now: datetime | None = None) -> bool:
    """Invalid, missing or out-of-window configuration disables recording."""
    try:
        start = date.fromisoformat(getattr(config, "THETA_AUDIT_START_DATE", ""))
        days = int(getattr(config, "THETA_AUDIT_DAYS", 30))
        now = now or _now()
        return (
            days > 0 and now.tzinfo is not None
            and start <= now.astimezone(ET).date() < start + timedelta(days=days)
        )
    except (ValueError, TypeError, OverflowError):
        return False


@dataclass(frozen=True)
class RequestTicket:
    request_id: str
    endpoint: str
    product: str
    purpose: str
    started_at: str
    session_date: str
    clock: float


def _append(ticket: RequestTicket, event: str, **fields) -> None:
    """Thread-safe within this process; persistence must never break a call."""
    record = {
        "schema": 1,
        "event": event,
        "request_id": ticket.request_id,
        "endpoint": ticket.endpoint,
        "product": ticket.product,
        "purpose": ticket.purpose,
        "started_at": ticket.started_at,
        **fields,
    }
    try:
        with _LOCK:
            directory = Path(THETADATA_DATA_DIR) / "audit"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"usage-{ticket.session_date}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        # Do not log exception text: paths/errors may contain private material.
        logger.warning("ThetaData usage metadata could not be persisted")


def begin_request(endpoint: str, purpose: str = "unspecified") -> RequestTicket | None:
    """Write before entering the SDK so interrupted calls remain observable."""
    try:
        now = _now()
        if endpoint not in _ENDPOINTS or not audit_active(now):
            return None
        ticket = RequestTicket(
            uuid.uuid4().hex, endpoint, _ENDPOINTS[endpoint],
            purpose if purpose in PURPOSES else "unspecified",
            now.astimezone(timezone.utc).isoformat(),
            now.astimezone(ET).date().isoformat(), time.perf_counter(),
        )
        _append(ticket, "start")
        return ticket
    except Exception:
        logger.warning("ThetaData usage metadata could not be started")
        return None


def finish_request(
    ticket: RequestTicket | None, *, rows: int | None = None,
    error: BaseException | None = None,
) -> None:
    """A success counts returned rows, not normalized/usable observations."""
    if ticket is None:
        return
    try:
        _append(
            ticket, "finish",
            finished_at=_now().astimezone(timezone.utc).isoformat(),
            elapsed_ms=round((time.perf_counter() - ticket.clock) * 1000, 3),
            success=error is None,
            error_class=type(error).__name__ if error is not None else None,
            rows=rows,
        )
    except Exception:
        logger.warning("ThetaData usage metadata could not be completed")
