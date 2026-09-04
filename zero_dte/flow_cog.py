"""Experimental, paper-only SPXW turning-point Discord watch.

This cog is isolated from the existing SPY forecast ensemble. It polls bounded
ThetaData windows, keeps raw OPRA records only in memory, and posts only
change-driven shadow observations. The provisional rules have not established
predictive or executable edge and must never be presented as trade advice.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import time

import discord
from discord.ext import commands, tasks
import pandas as pd

from config import (
    SPX_FLOW_ALERT_CHANNEL_ID,
    SPX_FLOW_ALERT_COOLDOWN_MINUTES,
    SPX_FLOW_LOOKBACK_MINUTES,
    SPX_FLOW_POLL_SECONDS,
    SPX_FLOW_STRIKE_RANGE,
)
from core import market_calendar
from core.market_data import MarketDataClient
from core.paths import THETADATA_DATA_DIR
from core.thetadata_client import IndexBar, OptionTradeQuote, ThetaDataClient
from zero_dte.turning_points import (
    ContractDecision,
    FlowSummary,
    KeyLevel,
    SessionOHLC,
    TurningPointAssessment,
    TurningState,
    assess_turning_point,
    build_key_levels,
    select_contract,
    summarize_option_flow,
)

logger = logging.getLogger(__name__)
ET = market_calendar.ET


class SPXFlowCog(commands.Cog, name="SPXFlow"):
    """Paper-only live observation of provisional SPX turning-point rules."""

    def __init__(
        self,
        bot: commands.Bot,
        market_client: MarketDataClient,
        theta_client: ThetaDataClient,
    ) -> None:
        self.bot = bot
        self.market_client = market_client
        self.theta_client = theta_client
        self._session_date: date | None = None
        self._levels: list[KeyLevel] = []
        self._recent_prices: deque[tuple[datetime, float]] = deque(maxlen=10)
        self._trades: dict[tuple, OptionTradeQuote] = {}
        self._assessment = _wait_assessment("WAIT: no live observation yet")
        self._last_flow = FlowSummary(0, 0, 0, 0, 0, 0, 0)
        self._last_price: float | None = None
        self._last_decision: ContractDecision | None = None
        self._last_sample_at: datetime | None = None
        self._potential_started_at: datetime | None = None
        self._last_spy_update: datetime | None = None
        self._vwap_z: float | None = None
        self._last_posted_key: tuple[str, float | None] | None = None
        self._last_posted_at: datetime | None = None
        self._paused_until: datetime | None = None
        self._consecutive_failures = 0
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.flow_monitor.change_interval(seconds=SPX_FLOW_POLL_SECONDS)
        self.flow_monitor.start()
        logger.info(
            "Experimental SPX flow monitor started (paper-only, every %ds)",
            SPX_FLOW_POLL_SECONDS,
        )

    async def cog_unload(self) -> None:
        self.flow_monitor.cancel()

    @commands.command(name="spxflow")
    async def cmd_spxflow(self, ctx: commands.Context) -> None:
        """Show a fresh experimental SPX flow observation."""
        now = datetime.now(ET)
        if not market_calendar.is_market_open(now):
            await ctx.send("SPX flow watch is paper-only and available only during market hours.")
            return
        if self._last_sample_at is None or self._last_price is None:
            await ctx.send("The experimental SPX flow monitor has not completed a live sample yet.")
            return
        age = (now - self._last_sample_at).total_seconds()
        if age > max(180, SPX_FLOW_POLL_SECONDS * 3):
            await ctx.send(f"The latest SPX flow sample is stale ({age / 60:.1f} minutes old).")
            return
        await ctx.send(embed=_flow_embed(
            self._assessment, self._last_flow, self._last_price,
            self._last_decision, self._last_sample_at,
        ))

    @tasks.loop(seconds=60)
    async def flow_monitor(self) -> None:
        now = datetime.now(ET)
        if not market_calendar.is_market_open(now):
            return
        grading_only = market_calendar.session_close(now) - now <= timedelta(
            minutes=30, seconds=SPX_FLOW_POLL_SECONDS
        )
        if self._paused_until is not None and now < self._paused_until:
            return
        # Avoid partial startup bars and a 09:30 history request ending before
        # its own start time.
        if now < now.replace(hour=9, minute=32, second=0, microsecond=0):
            return
        try:
            if grading_only:
                await self._grade_only(now)
                self._consecutive_failures = 0
                self._paused_until = None
                return
            assessment, flow, price, decision = await self._sample(now)
            self._consecutive_failures = 0
            self._paused_until = None
        except Exception as exc:
            self._consecutive_failures += 1
            logger.exception("Experimental SPX flow poll failed: %s", exc)
            if self._consecutive_failures >= 3:
                self._paused_until = now + timedelta(minutes=5)
                logger.error("SPX flow circuit breaker paused polling until %s", self._paused_until)
            return
        if assessment.state == TurningState.WAIT:
            self._last_posted_key = None
            return
        level_price = assessment.level.price if assessment.level else None
        post_key = (assessment.state.value, level_price)
        if post_key == self._last_posted_key:
            return
        confirmed = assessment.is_confirmed_reversal
        cooldown_elapsed = (
            self._last_posted_at is None
            or now - self._last_posted_at >= timedelta(minutes=SPX_FLOW_ALERT_COOLDOWN_MINUTES)
        )
        if not confirmed and not cooldown_elapsed:
            return
        channel = self.bot.get_channel(SPX_FLOW_ALERT_CHANNEL_ID)
        if channel is None:
            logger.error("SPX flow alert channel %s not found", SPX_FLOW_ALERT_CHANNEL_ID)
            return
        # Persist before delivery so every emitted message is gradeable. Set
        # duplicate suppression first so a half-failed delivery cannot fire the
        # same state repeatedly.
        self._last_posted_key = post_key
        self._last_posted_at = now
        try:
            self._record_posted_alert(now, price, assessment, decision)
        except Exception as exc:
            self._last_posted_key = None
            logger.exception("SPX flow persistence failed; Discord message suppressed: %s", exc)
            return
        try:
            await channel.send(embed=_flow_embed(assessment, flow, price, decision, now))
        except Exception as exc:
            logger.exception("SPX flow Discord delivery failed; duplicate retry suppressed: %s", exc)

    @flow_monitor.before_loop
    async def before_flow_monitor(self) -> None:
        await self.bot.wait_until_ready()

    @flow_monitor.error
    async def flow_monitor_error(self, exc: BaseException) -> None:
        logger.exception("SPX flow monitor crashed: %s — restarting in 30s", exc)
        await asyncio.sleep(30)
        self.flow_monitor.restart()

    async def _sample(
        self, now: datetime
    ) -> tuple[TurningPointAssessment, FlowSummary, float, ContractDecision | None]:
        async with self._lock:
            price, snapshot_timestamp = await self._current_spx(now)
            if self._session_date != now.date():
                await self._start_session(now, price)
            if not self._recent_prices or self._recent_prices[-1][0] != snapshot_timestamp:
                self._recent_prices.append((snapshot_timestamp, price))

            end = now - timedelta(seconds=5)
            session_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            start = max(session_open, end - timedelta(minutes=2))
            new_trades = await _theta_call(
                "SPXW two-minute trade history", self.theta_client.option_trade_quotes,
                now.date(),
                symbol="SPXW",
                expiration=now.date(),
                strike_range=SPX_FLOW_STRIKE_RANGE,
                start_time=start.strftime("%H:%M:%S"),
                end_time=end.strftime("%H:%M:%S"),
            )
            self._merge_trades(new_trades, now)
            flow_start = now.astimezone(timezone.utc) - timedelta(minutes=SPX_FLOW_LOOKBACK_MINUTES)
            flow_trades = [
                trade for trade in self._trades.values()
                if flow_start <= trade.trade_timestamp.astimezone(timezone.utc) <= now.astimezone(timezone.utc)
            ]
            flow = summarize_option_flow(flow_trades)

            if (
                self._last_spy_update is None
                or now - self._last_spy_update >= timedelta(minutes=5)
            ):
                candles = await self.market_client.fetch_candles("SPY", interval="1", count=390)
                self._vwap_z = _spy_vwap_z(candles, now)
                self._last_spy_update = now

            previous = self._assessment
            episode_live = (
                self._potential_started_at is not None
                and now - self._potential_started_at <= timedelta(minutes=10)
            )
            previous_state = previous.state if episode_live else TurningState.WAIT
            watched_level = previous.level if episode_live else None
            cutoff = now.astimezone(timezone.utc) - timedelta(minutes=5)
            recent_observations = [
                (stamp, value) for stamp, value in self._recent_prices
                if stamp.astimezone(timezone.utc) >= cutoff
            ]
            # Invocation count is not elapsed time: require roughly four
            # minutes of timestamped observations before calling it 5m momentum.
            window_ready = (
                recent_observations
                and recent_observations[0][0].astimezone(timezone.utc)
                <= now.astimezone(timezone.utc) - timedelta(minutes=4)
            )
            recent_prices = [value for _, value in recent_observations] if window_ready else []
            self._assessment = assess_turning_point(
                price=price,
                recent_prices=recent_prices,
                vwap_z=self._vwap_z,
                flow=flow,
                levels=self._levels,
                previous_state=previous_state,
                watched_level=watched_level,
            )
            if self._assessment.state in {TurningState.POTENTIAL_TOP, TurningState.POTENTIAL_BOTTOM}:
                if previous_state != self._assessment.state or self._potential_started_at is None:
                    self._potential_started_at = now
            else:
                self._potential_started_at = None
            decision: ContractDecision | None = None
            if self._assessment.is_confirmed_reversal:
                direction = "put" if self._assessment.state == TurningState.TOP_CONFIRMED else "call"
                try:
                    chain = await _theta_call(
                        "SPXW paper-candidate chain", self.theta_client.option_chain_snapshot,
                        now.date(),
                        symbol="SPXW",
                        right=direction,
                        strike_range=10,
                        include_details=True,
                    )
                    decision = select_contract(
                        chain,
                        direction=direction,
                        spx_price=price,
                        now=now,
                        market_close=market_calendar.session_close(now),
                    )
                except Exception as exc:
                    logger.warning("Paper contract lookup failed; state remains observable: %s", exc)
                    decision = ContractDecision(None, "WAIT: option snapshot unavailable")
            self._last_flow = flow
            self._last_price = price
            self._last_decision = decision
            self._last_sample_at = now
            self._write_shadow(now, price, flow, self._assessment, decision)
            await self._grade_pending_alerts(now, price)
            return self._assessment, flow, price, decision

    async def _current_spx(self, now: datetime) -> tuple[float, datetime]:
        snapshots = await _theta_call(
            "SPX index snapshot", self.theta_client.index_snapshots, ("SPX",)
        )
        spx = next((item for item in snapshots if item.symbol == "SPX"), None)
        if spx is None or spx.price <= 0:
            raise RuntimeError("ThetaData returned no valid SPX snapshot")
        if spx.timestamp is None or spx.timestamp.tzinfo is None:
            raise RuntimeError("ThetaData SPX snapshot has no timezone-aware timestamp")
        age = (now.astimezone(timezone.utc) - spx.timestamp.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > 90:
            raise RuntimeError(f"ThetaData SPX snapshot is stale ({age:.1f}s)")
        return spx.price, spx.timestamp

    async def _grade_only(self, now: datetime) -> None:
        """Finish due outcomes after new-alert creation stops for the day."""
        async with self._lock:
            price, _ = await self._current_spx(now)
            await self._grade_pending_alerts(now, price)

    def _alerts_path(self, session_date: date) -> str:
        directory = os.path.join(THETADATA_DATA_DIR, "shadow")
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"spx-flow-alerts-{session_date.isoformat()}.json")

    def _load_alerts(self, session_date: date) -> list[dict]:
        path = self._alerts_path(session_date)
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not load SPX flow shadow alerts: %s", exc)
            return []

    def _save_alerts(self, session_date: date, rows: list[dict]) -> None:
        path = self._alerts_path(session_date)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)

    def _record_posted_alert(
        self,
        now: datetime,
        price: float,
        assessment: TurningPointAssessment,
        decision: ContractDecision | None,
    ) -> None:
        if assessment.state in {TurningState.POTENTIAL_TOP, TurningState.TOP_CONFIRMED}:
            expected_direction = "DOWN"
        elif assessment.state in {TurningState.POTENTIAL_BOTTOM, TurningState.BOTTOM_CONFIRMED}:
            expected_direction = "UP"
        else:
            expected_direction = "UP" if assessment.momentum_points > 0 else "DOWN"
        contract = decision.contract if decision else None
        rows = self._load_alerts(now.date())
        rows.append({
            "id": now.astimezone(timezone.utc).isoformat(),
            "paper_only": True,
            "state": assessment.state.value,
            "expected_direction": expected_direction,
            "entry_spx": price,
            "level": asdict(assessment.level) if assessment.level else None,
            "entry_ask": contract.ask if contract else None,
            "contract": ({
                "symbol": contract.symbol,
                "expiration": contract.expiration.isoformat(),
                "strike": contract.strike,
                "right": contract.right,
            } if contract else None),
            "outcomes": {},
        })
        self._save_alerts(now.date(), rows)

    async def _grade_pending_alerts(self, now: datetime, spx_price: float) -> None:
        rows = self._load_alerts(now.date())
        changed = False
        for row in rows:
            try:
                entry_time = datetime.fromisoformat(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            outcomes = row.setdefault("outcomes", {})
            for minutes in (15, 30):
                key = str(minutes)
                target = entry_time + timedelta(minutes=minutes)
                existing = outcomes.get(key)
                if (existing is not None and not existing.get("contract_pending", False)):
                    continue
                if now.astimezone(timezone.utc) < target.astimezone(timezone.utc):
                    continue
                if existing is None:
                    move = spx_price - float(row["entry_spx"])
                    expected = row.get("expected_direction")
                    outcome = {
                        "observed_at": now.astimezone(timezone.utc).isoformat(),
                        "spx_exit": spx_price,
                        "spx_move": round(move, 4),
                        "direction_correct": move > 0 if expected == "UP" else move < 0,
                        "contract_exit_bid": None,
                        "contract_return_pct": None,
                        "contract_pending": False,
                    }
                else:
                    outcome = existing
                contract = row.get("contract")
                entry_ask = row.get("entry_ask")
                if contract and entry_ask:
                    target_et = target.astimezone(ET)
                    end_et = min(now, target_et + timedelta(minutes=2))
                    outcome["contract_pending"] = now < target_et + timedelta(minutes=2)
                    try:
                        quotes = await _theta_call(
                            "SPXW exact-contract outcome", self.theta_client.option_quote_history,
                            now.date(),
                            expiration=date.fromisoformat(contract["expiration"]),
                            strike=float(contract["strike"]),
                            right=contract["right"],
                            symbol=contract["symbol"],
                            interval="1s",
                            start_time=target_et.strftime("%H:%M:%S"),
                            end_time=end_et.strftime("%H:%M:%S"),
                        )
                        exit_quote = next((
                            quote for quote in quotes
                            if _valid_exit_quote(quote, contract, target)
                        ), None)
                        if exit_quote is not None:
                            quote_time = exit_quote.timestamp.astimezone(timezone.utc)
                            outcome["contract_exit_bid"] = exit_quote.bid
                            outcome["contract_exit_quote_time"] = quote_time.isoformat()
                            outcome["contract_exit_delay_seconds"] = round(
                                (quote_time - target.astimezone(timezone.utc)).total_seconds(), 3
                            )
                            outcome["contract_return_pct"] = round(
                                (exit_quote.bid - float(entry_ask)) / float(entry_ask) * 100, 2
                            )
                            outcome["contract_pending"] = False
                    except Exception as exc:
                        outcome["contract_pending"] = True
                        logger.warning("SPX flow contract outcome lookup failed: %s", exc)
                outcomes[key] = outcome
                changed = True
        if changed:
            self._save_alerts(now.date(), rows)

    async def _start_session(self, now: datetime, price: float) -> None:
        prior_date = now.date() - timedelta(days=1)
        while not market_calendar.is_trading_day(prior_date):
            prior_date -= timedelta(days=1)
        end_time = (now - timedelta(seconds=5)).strftime("%H:%M:%S")
        today_bars: list[IndexBar] = await _theta_call(
            "SPX current-session levels", self.theta_client.index_history_ohlc,
            now.date(),
            symbol="SPX",
            interval="1m",
            end_time=end_time,
        )
        prior_bars: list[IndexBar] = await _theta_call(
            "SPX prior-session levels", self.theta_client.index_history_ohlc,
            prior_date,
            symbol="SPX",
            interval="1m",
            end_time=_close_string(prior_date),
        )
        prior = _complete_session_ohlc(prior_date, prior_bars)
        prior_sessions = [prior] if prior is not None else []
        if prior is None:
            logger.warning("Prior SPX session coverage is incomplete; daily levels omitted")
        session_open = today_bars[0].open if today_bars else price
        self._levels = build_key_levels(
            [bar for bar in prior_sessions if bar is not None],
            session_date=now.date(),
            session_open=session_open,
            reference_price=price,
        )
        self._session_date = now.date()
        self._recent_prices.clear()
        self._trades.clear()
        self._assessment = _wait_assessment("WAIT: new session")
        self._potential_started_at = None
        self._last_spy_update = None
        self._vwap_z = None
        self._last_posted_key = None
        logger.info("Initialized experimental SPX flow session with %d known levels", len(self._levels))

    def _merge_trades(self, trades: list[OptionTradeQuote], now: datetime) -> None:
        for trade in trades:
            identity = (
                trade.symbol, trade.expiration, trade.strike, trade.right,
                trade.trade_timestamp, trade.sequence, trade.exchange,
            )
            self._trades[identity] = trade
        cutoff = now.astimezone(timezone.utc) - timedelta(minutes=10)
        self._trades = {
            key: trade for key, trade in self._trades.items()
            if trade.trade_timestamp.tzinfo is not None
            and trade.trade_timestamp.astimezone(timezone.utc) >= cutoff
        }

    @staticmethod
    def _write_shadow(
        now: datetime,
        price: float,
        flow: FlowSummary,
        assessment: TurningPointAssessment,
        decision: ContractDecision | None,
    ) -> None:
        directory = os.path.join(THETADATA_DATA_DIR, "shadow")
        os.makedirs(directory, exist_ok=True)
        contract = decision.contract if decision else None
        record = {
            "time": now.astimezone(timezone.utc).isoformat(),
            "paper_only": True,
            "spx_price": price,
            "state": assessment.state.value,
            "level": asdict(assessment.level) if assessment.level else None,
            "momentum_points": assessment.momentum_points,
            "vwap_z": assessment.vwap_z,
            "flow_ratio": assessment.flow_ratio,
            "classified_pct": flow.classified_pct,
            "flow_trades": flow.total_trades,
            "decision": decision.reason if decision else "No contract requested",
            "contract": ({
                "symbol": contract.symbol,
                "expiration": contract.expiration.isoformat(),
                "strike": contract.strike,
                "right": contract.right,
                "bid": contract.bid,
                "ask": contract.ask,
                "spread_pct": contract.spread_pct,
                "delta": contract.delta,
            } if contract else None),
        }
        path = os.path.join(directory, f"spx-flow-{now.date().isoformat()}.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


async def _theta_call(label: str, function, *args, **kwargs):
    started = time.perf_counter()
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    finally:
        elapsed = time.perf_counter() - started
        log = logger.warning if elapsed >= 15 else logger.debug
        log("ThetaData %s completed in %.2fs", label, elapsed)


def _valid_exit_quote(quote, contract: dict, target: datetime) -> bool:
    stamp = quote.timestamp
    if stamp.tzinfo is None or target.tzinfo is None:
        return False
    delay = (stamp.astimezone(timezone.utc) - target.astimezone(timezone.utc)).total_seconds()
    return (
        0 <= delay <= 120
        and quote.symbol == contract.get("symbol")
        and quote.expiration.isoformat() == contract.get("expiration")
        and abs(quote.strike - float(contract.get("strike", float("nan")))) < 1e-9
        and quote.right == contract.get("right")
        and quote.bid > 0
        and quote.ask >= quote.bid
        and quote.ask > 0
        and quote.bid_size > 0
    )


def _session_ohlc(session_date: date, bars: list[IndexBar]) -> SessionOHLC | None:
    if not bars:
        return None
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    return SessionOHLC(
        session_date=session_date,
        open=ordered[0].open,
        high=max(bar.high for bar in ordered),
        low=min(bar.low for bar in ordered),
        close=ordered[-1].close,
    )


def _complete_session_ohlc(session_date: date, bars: list[IndexBar]) -> SessionOHLC | None:
    if not bars or any(bar.timestamp.tzinfo is None for bar in bars):
        return None
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    local = [bar.timestamp.astimezone(ET) for bar in ordered]
    close_hour, close_minute = market_calendar.close_time(session_date)
    expected_open = ET.localize(datetime(
        session_date.year, session_date.month, session_date.day, 9, 30
    ))
    expected_last = ET.localize(datetime(
        session_date.year, session_date.month, session_date.day, close_hour, close_minute
    )) - timedelta(minutes=1)
    tolerance = timedelta(minutes=2)
    expected_rows = int((expected_last - expected_open).total_seconds() // 60) + 1
    unique_local = sorted(set(local))
    material_gap = any(
        later - earlier > timedelta(minutes=2)
        for earlier, later in zip(unique_local, unique_local[1:])
    )
    if (
        any(stamp.date() != session_date for stamp in local)
        or abs(local[0] - expected_open) > tolerance
        or local[-1] < expected_last - tolerance
        or len(unique_local) < expected_rows - 2
        or material_gap
    ):
        return None
    return _session_ohlc(session_date, ordered)


def _close_string(session_date: date) -> str:
    hour, minute = market_calendar.close_time(session_date)
    return f"{hour:02d}:{minute:02d}:00"


def _spy_vwap_z(candles: pd.DataFrame, now: datetime) -> float | None:
    """Current completed-bar SPY distance from session VWAP in weighted sigmas."""
    if candles is None or candles.empty:
        return None
    frame = candles.copy()
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    timestamps = timestamps.dt.tz_convert(ET)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    complete = timestamps + pd.Timedelta(minutes=1) <= pd.Timestamp(now)
    mask = (timestamps.dt.date == now.date()) & (timestamps >= open_time) & complete
    frame = frame.loc[mask.values].copy()
    if len(frame) < 5:
        return None
    typical = (
        pd.to_numeric(frame["high"], errors="coerce")
        + pd.to_numeric(frame["low"], errors="coerce")
        + pd.to_numeric(frame["close"], errors="coerce")
    ) / 3.0
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0).clip(lower=0)
    valid = typical.notna() & volume.gt(0)
    if valid.sum() < 5:
        return None
    typical = typical[valid]
    volume = volume[valid]
    total_volume = float(volume.sum())
    vwap = float((typical * volume).sum() / total_volume)
    variance = float((((typical - vwap) ** 2) * volume).sum() / total_volume)
    if variance <= 0:
        return None
    return round((float(typical.iloc[-1]) - vwap) / variance ** 0.5, 3)


def _wait_assessment(reason: str) -> TurningPointAssessment:
    return TurningPointAssessment(TurningState.WAIT, None, 0.0, None, 0.0, reason)


def _flow_embed(
    assessment: TurningPointAssessment,
    flow: FlowSummary,
    price: float,
    decision: ContractDecision | None,
    now: datetime,
) -> discord.Embed:
    colors = {
        TurningState.POTENTIAL_TOP: 0xF1C40F,
        TurningState.POTENTIAL_BOTTOM: 0xF1C40F,
        TurningState.TOP_CONFIRMED: 0xE74C3C,
        TurningState.BOTTOM_CONFIRMED: 0x2ECC71,
        TurningState.TREND_CONTINUATION: 0x3498DB,
        TurningState.WAIT: 0x95A5A6,
    }
    labels = {
        TurningState.POTENTIAL_TOP: "Potential top — not confirmed",
        TurningState.POTENTIAL_BOTTOM: "Potential bottom — not confirmed",
        TurningState.TOP_CONFIRMED: "Experimental top rejection",
        TurningState.BOTTOM_CONFIRMED: "Experimental bottom bounce",
        TurningState.TREND_CONTINUATION: "Trend continuation — do not fade",
        TurningState.WAIT: "WAIT",
    }
    embed = discord.Embed(
        title="🧪 Experimental SPX Turning-Point Watch",
        description=f"**{labels[assessment.state]}**\n{assessment.reason}",
        color=colors[assessment.state],
        timestamp=now,
    )
    level = assessment.level
    embed.add_field(name="SPX", value=f"{price:,.2f}", inline=True)
    embed.add_field(
        name="Level",
        value=f"{level.price:,.2f} — {level.label}" if level else "None",
        inline=True,
    )
    momentum_label = "Rising" if assessment.momentum_points > 0 else "Falling" if assessment.momentum_points < 0 else "Flat"
    embed.add_field(
        name="Momentum",
        value=f"{momentum_label} ({assessment.momentum_points:+.2f} points)",
        inline=True,
    )
    flow_label = "Bullish" if assessment.flow_ratio > 0.05 else "Bearish" if assessment.flow_ratio < -0.05 else "Mixed"
    embed.add_field(
        name="Option flow",
        value=(f"{flow_label} ({assessment.flow_ratio:+.1%})\n"
               f"{flow.classified_pct:.1f}% classifiable, {flow.total_trades:,} prints"),
        inline=True,
    )
    embed.add_field(
        name="SPY VWAP extension",
        value=f"{assessment.vwap_z:+.2f}σ" if assessment.vwap_z is not None else "Unavailable",
        inline=True,
    )
    if decision is not None:
        contract = decision.contract
        if contract is None:
            embed.add_field(name="Paper decision", value=decision.reason, inline=False)
        else:
            right = "Call" if contract.right == "call" else "Put"
            embed.add_field(
                name="Paper candidate — not a recommendation",
                value=(
                    f"SPXW {contract.strike:g} {right}\n"
                    f"Bid/Ask: ${contract.bid:.2f} / ${contract.ask:.2f} "
                    f"(${contract.ask * 100:,.0f} debit)\n"
                    f"Spread: {contract.spread_pct:.1f}% | Delta: {contract.delta:+.2f}\n"
                    "Shadow evaluation: 15 and 30 minutes"
                ),
                inline=False,
            )
    else:
        embed.add_field(name="Decision", value="WAIT — no contract before a rejection is observed", inline=False)
    embed.set_footer(text="PAPER-ONLY • provisional rules • predictive edge not established")
    return embed
