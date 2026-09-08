"""Month-bounded local SPX ORB paper sampler; explicitly installed by the owner.

AuditCog(bot, ThetaDataClient(purpose="audit")) is the integration interface.
No alerts, commands, order submission, option calls, or changes to other cogs.
State is single-writer, atomic/restart-safe, and licensed data stays ignored.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path

from discord.ext import commands, tasks

from core import market_calendar
from core.paths import THETADATA_DATA_DIR
from core.theta_usage import audit_active
from zero_dte.orb_audit import (
    evaluate_outcome, new_record, session_time, update_record, utc, validate_record,
)

logger = logging.getLogger(__name__)


def collection_open(now: datetime) -> bool:
    """Calendar helpers expect ET; allow five minutes to collect the last bar."""
    local = now.astimezone(market_calendar.ET)
    if not market_calendar.is_trading_day(local.date()):
        return False
    opening = local.replace(hour=9, minute=31, second=0, microsecond=0)
    return opening <= local <= market_calendar.session_close(local) + timedelta(minutes=5)


def capture_flow(bot, captured_at: datetime) -> dict | None:
    """Copy an already completed cache before awaiting network I/O.

    SPXFlow's sample timestamp is its poll-start time, so its age is conservative.
    No price/flow cache access causes any additional option request.
    """
    cog = bot.get_cog("SPXFlow")
    if cog is None:
        return None
    flow = getattr(cog, "_last_flow", None)
    stamp = getattr(cog, "_last_sample_at", None)
    if flow is None or stamp is None:
        return None
    components = (flow.call_ask_premium + flow.call_bid_premium
                  + flow.put_ask_premium + flow.put_bid_premium)
    latest = getattr(cog, "_last_trade_at", None)
    return {"sample_at": stamp.isoformat(), "captured_at": captured_at.isoformat(),
            "request_id": getattr(cog, "_last_flow_request_id", None),
            "request_ids": list(getattr(cog, "_last_flow_request_ids", [])),
            "latest_trade_at": latest.isoformat() if latest is not None else None,
            "new_trade_rows": getattr(cog, "_last_new_trade_rows", None),
            "total_trades": flow.total_trades, "classified_trades": flow.classified_trades,
            "ratio": flow.net_directional_premium / components if components > 0 else 0.0}


class AuditCog(commands.Cog, name="SPXORBAudit"):
    def __init__(self, bot, theta_client, *, directory: str | Path | None = None):
        self.bot = bot
        self.theta_client = theta_client
        self.directory = Path(directory) if directory is not None else Path(THETADATA_DATA_DIR) / "audit"
        self._lock = asyncio.Lock()
        self._reconciled_day = None

    async def cog_load(self):
        self.audit_monitor.start()

    async def cog_unload(self):
        self.audit_monitor.cancel()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _path(self, day):
        return self.directory / f"orb-{day.isoformat()}.json"

    def _load(self, day):
        path = self._path(day)
        if not path.exists():
            return new_record(day)
        with path.open(encoding="utf-8") as handle:
            record = json.load(handle)
        validate_record(record, day)
        return record

    def _save(self, day, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(day)
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _reconcile_previous(self, today, now):
        """Resolve interrupted older candidates from saved evidence, never refetch.

        Only records in the current configured audit window are touched. An
        overnight outage produces explicit missing outcomes, not silent attrition.
        """
        if self._reconciled_day == today:
            return
        succeeded = True
        for path in self.directory.glob("orb-????-??-??.json"):
            if not audit_active(self._now()):
                return
            try:
                day = date.fromisoformat(path.stem.removeprefix("orb-"))
                if day >= today or not audit_active(session_time(day, 9, 30)):
                    continue
                record = self._load(day)
                candidate = record.get("candidate")
                if not candidate or candidate["status"] != "prospective" or record["outcome"] is not None:
                    continue
                known = {utc(b["timestamp"]): b for b in record["bars"]}
                outcome = evaluate_outcome(candidate, known, now)
                if outcome is not None:
                    record["outcome"] = outcome
                    record["status"] = outcome["status"]
                    record["reconciled_at"] = now.isoformat()
                    self._save(day, record)
            except Exception:
                succeeded = False
                logger.warning("SPX ORB prior-session reconciliation failed")
        if succeeded:
            self._reconciled_day = today

    async def poll_once(self, now: datetime | None = None):
        """One bounded poll; public for deterministic offline integration tests."""
        now = now or self._now()
        if not audit_active(now) or not collection_open(now):
            return
        async with self._lock:
            # Recheck after lock contention; never begin an expired request.
            admitted = self._now()
            if not audit_active(admitted) or not collection_open(admitted):
                return
            day = admitted.astimezone(market_calendar.ET).date()
            self._reconcile_previous(day, admitted)
            try:
                record = self._load(day)
                record["polls"] += 1
                record["last_poll_at"] = admitted.isoformat()
                record["poll_status"] = "request_started"
                # A durable heartbeat before SDK entry makes pending/interrupted
                # polls distinguishable from ordinary empty responses.
                self._save(day, record)
            except Exception:
                logger.warning("SPX ORB audit state unavailable; poll suppressed")
                return  # Do not overwrite damaged evidence with a new session.
            try:
                request_at = self._now()
                if not audit_active(request_at) or not collection_open(request_at):
                    return
                flow = capture_flow(self.bot, request_at)
                local = admitted.astimezone(market_calendar.ET)
                coverage_end = min(local.replace(second=0, microsecond=0),
                                   market_calendar.session_close(local))
                # Request from the earliest missing minute. Normal polls fetch
                # only new bars; restart/gap recovery never silently skips holes.
                known = {utc(bar["timestamp"]) for bar in record["bars"]}
                start_time = session_time(day, 9, 30)
                while start_time in known and start_time < coverage_end:
                    start_time += timedelta(minutes=1)
                # After a complete close, retain a last-bar overlap rather than
                # sending a zero-width history request during the close grace.
                start_time = min(start_time, coverage_end - timedelta(minutes=1))
                bars = await asyncio.to_thread(
                    self.theta_client.index_history_ohlc, day, symbol="SPX",
                    interval="1m", start_time=start_time.astimezone(market_calendar.ET).strftime("%H:%M:%S"),
                    end_time=coverage_end.strftime("%H:%M:%S"),
                )
                received = self._now()
                if (not audit_active(received)
                        or received.astimezone(market_calendar.ET).date() != day):
                    # Keep the start heartbeat; don't create a late next-day
                    # decision or write licensed payload after the audit expires.
                    return
                update_record(record, bars, received, flow,
                              data_cutoff=coverage_end)
                record["poll_status"] = "success"
            except Exception:
                record["fetch_failures"] += 1
                record["poll_status"] = "failed"
                logger.warning("SPX ORB audit poll failed")  # No exception text/secrets.
            # The same completion policy applies to failures and successes.
            if not audit_active(self._now()):
                return
            try:
                self._save(day, record)
            except Exception:
                logger.warning("SPX ORB audit persistence failed")

    @tasks.loop(seconds=60)
    async def audit_monitor(self):
        await self.poll_once()

    @audit_monitor.before_loop
    async def before_audit_monitor(self):
        await self.bot.wait_until_ready()

    @audit_monitor.error
    async def audit_monitor_error(self, _exc: BaseException):
        logger.warning("SPX ORB audit loop interrupted; restarting in 30 seconds")
        await asyncio.sleep(30)
        self.audit_monitor.restart()
