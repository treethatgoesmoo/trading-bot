"""Pure research primitives for the proposed SPXW turning-point system.

Nothing in this module claims that a market top or bottom is known.  OPRA
trade/quote data can support a failed-follow-through proxy, but it cannot show
the full book or identify every hedge and multi-leg order.  These functions are
kept network-free so the exact rules can be replayed before any Discord output
is considered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
import math
from typing import Iterable, Sequence

from core.market_calendar import ET, is_trading_day
from core.thetadata_client import OptionSnapshot, OptionTradeQuote


class Aggressor(str, Enum):
    ASK = "ASK"
    BID = "BID"
    MID = "MID"
    INVALID = "INVALID"


class TurningState(str, Enum):
    """Paper-only state labels; none claims the exact session extreme."""

    WAIT = "WAIT"
    POTENTIAL_TOP = "POTENTIAL_TOP"
    TOP_CONFIRMED = "TOP_CONFIRMED"
    POTENTIAL_BOTTOM = "POTENTIAL_BOTTOM"
    BOTTOM_CONFIRMED = "BOTTOM_CONFIRMED"
    TREND_CONTINUATION = "TREND_CONTINUATION"


@dataclass(frozen=True)
class SessionOHLC:
    session_date: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class KeyLevel:
    price: float
    label: str
    kind: str


@dataclass(frozen=True)
class FlowSummary:
    total_trades: int
    classified_trades: int
    call_ask_premium: float
    call_bid_premium: float
    put_ask_premium: float
    put_bid_premium: float
    net_directional_premium: float

    @property
    def classified_pct(self) -> float:
        return self.classified_trades / self.total_trades * 100.0 if self.total_trades else 0.0


@dataclass(frozen=True)
class ProvisionalTurningThresholds:
    """Explicit starting rules for live shadow observation, not tuned values."""

    level_proximity_points: float = 2.5
    vwap_extension_z: float = 3.0
    minimum_momentum_points: float = 0.5
    strong_flow_ratio: float = 0.15
    reversal_flow_ratio: float = 0.05
    minimum_classified_pct: float = 20.0
    minimum_classified_trades: int = 25
    rejection_points: float = 1.0
    acceptance_points: float = 2.0


@dataclass(frozen=True)
class TurningPointAssessment:
    state: TurningState
    level: KeyLevel | None
    momentum_points: float
    vwap_z: float | None
    flow_ratio: float
    reason: str

    @property
    def is_confirmed_reversal(self) -> bool:
        return self.state in {TurningState.TOP_CONFIRMED, TurningState.BOTTOM_CONFIRMED}


@dataclass(frozen=True)
class ContractPolicy:
    max_ask: float = 10.0
    target_abs_delta: float = 0.50
    min_abs_delta: float = 0.30
    max_abs_delta: float = 0.65
    max_spread_pct: float = 12.0
    max_quote_age_seconds: float = 10.0
    max_greek_age_seconds: float = 10.0
    minimum_minutes_remaining: int = 30
    minimum_size: int = 1


@dataclass(frozen=True)
class ContractDecision:
    contract: OptionSnapshot | None
    reason: str

    @property
    def should_wait(self) -> bool:
        return self.contract is None


def infer_aggressor(trade: OptionTradeQuote, *, max_quote_age_seconds: float = 2.0) -> Aggressor:
    """Infer trade side with the quote-midpoint rule.

    A trade above the paired NBBO midpoint is treated as buyer initiated; one
    below it as seller initiated.  Exact-midpoint, crossed, locked, future, or
    stale quotes are excluded rather than forced into a direction.
    """
    if trade.price <= 0 or trade.size <= 0 or trade.bid < 0 or trade.ask <= trade.bid:
        return Aggressor.INVALID
    if trade.quote_timestamp is None:
        return Aggressor.INVALID
    quote_stamp = trade.quote_timestamp
    trade_stamp = trade.trade_timestamp
    if quote_stamp.tzinfo is None or trade_stamp.tzinfo is None:
        return Aggressor.INVALID
    age = (trade_stamp - quote_stamp).total_seconds()
    if age < 0 or age > max_quote_age_seconds:
        return Aggressor.INVALID
    midpoint = (trade.bid + trade.ask) / 2.0
    epsilon = 1e-9
    if trade.price > midpoint + epsilon:
        return Aggressor.ASK
    if trade.price < midpoint - epsilon:
        return Aggressor.BID
    return Aggressor.MID


def summarize_option_flow(
    trades: Iterable[OptionTradeQuote], *, max_quote_age_seconds: float = 2.0,
    allowed_condition_codes: frozenset[int] = frozenset({0}),
) -> FlowSummary:
    """Aggregate classified call/put premium into an inferred direction.

    Positive net premium is bullish: call buying and put selling contribute
    positively; call selling and put buying contribute negatively.  This is a
    research feature, not proof that positions were opened or closed.
    """
    buckets = {("call", Aggressor.ASK): 0.0, ("call", Aggressor.BID): 0.0,
               ("put", Aggressor.ASK): 0.0, ("put", Aggressor.BID): 0.0}
    total = classified = 0
    for trade in trades:
        total += 1
        # Until the frozen OPRA condition profile is reviewed, only ordinary
        # condition-0 prints may contribute directional premium. Complex,
        # corrected, auction, and unknown prints remain in the quality count.
        if trade.condition not in allowed_condition_codes:
            continue
        side = infer_aggressor(trade, max_quote_age_seconds=max_quote_age_seconds)
        key = (trade.right, side)
        if key not in buckets:
            continue
        classified += 1
        buckets[key] += trade.price * trade.size * 100.0
    call_ask = buckets[("call", Aggressor.ASK)]
    call_bid = buckets[("call", Aggressor.BID)]
    put_ask = buckets[("put", Aggressor.ASK)]
    put_bid = buckets[("put", Aggressor.BID)]
    net = call_ask + put_bid - call_bid - put_ask
    return FlowSummary(total, classified, call_ask, call_bid, put_ask, put_bid, net)


def assess_turning_point(
    *,
    price: float,
    recent_prices: Sequence[float],
    vwap_z: float | None,
    flow: FlowSummary,
    levels: Sequence[KeyLevel],
    previous_state: TurningState = TurningState.WAIT,
    watched_level: KeyLevel | None = None,
    thresholds: ProvisionalTurningThresholds = ProvisionalTurningThresholds(),
) -> TurningPointAssessment:
    """Apply provisional, paper-only turning-point state transitions.

    A potential state needs level contact, a 3-sigma VWAP extension, momentum
    into the level, and aligned inferred option flow. A confirmed reversal can
    only follow the matching potential state at the same watched level. Moving
    through that level with continued momentum is continuation, not a fade.
    """
    usable_prices = [float(value) for value in recent_prices if math.isfinite(float(value))]
    momentum = price - usable_prices[0] if usable_prices else 0.0
    components = (
        flow.call_ask_premium + flow.call_bid_premium
        + flow.put_ask_premium + flow.put_bid_premium
    )
    flow_ratio = flow.net_directional_premium / components if components > 0 else 0.0

    def result(state: TurningState, level: KeyLevel | None, reason: str) -> TurningPointAssessment:
        return TurningPointAssessment(
            state, level, round(momentum, 4), vwap_z, round(flow_ratio, 4), reason
        )

    if not math.isfinite(price) or price <= 0:
        return result(TurningState.WAIT, None, "WAIT: invalid SPX price")
    if previous_state in {TurningState.POTENTIAL_TOP, TurningState.POTENTIAL_BOTTOM}:
        if watched_level is None:
            return result(TurningState.WAIT, None, "WAIT: potential state has no watched level")
        level_price = watched_level.price
        if previous_state == TurningState.POTENTIAL_TOP:
            reversed_read = (
                momentum <= -thresholds.minimum_momentum_points
                or flow_ratio <= -thresholds.reversal_flow_ratio
            )
            if price <= level_price - thresholds.rejection_points and reversed_read:
                return result(
                    TurningState.TOP_CONFIRMED, watched_level,
                    "Paper-only rejection: price returned below resistance with momentum/flow reversal",
                )
            if (price >= level_price + thresholds.acceptance_points
                    and momentum >= thresholds.minimum_momentum_points):
                return result(
                    TurningState.TREND_CONTINUATION, watched_level,
                    "Price accepted above resistance; do not fade the move",
                )
        else:
            reversed_read = (
                momentum >= thresholds.minimum_momentum_points
                or flow_ratio >= thresholds.reversal_flow_ratio
            )
            if price >= level_price + thresholds.rejection_points and reversed_read:
                return result(
                    TurningState.BOTTOM_CONFIRMED, watched_level,
                    "Paper-only bounce: price returned above support with momentum/flow reversal",
                )
            if (price <= level_price - thresholds.acceptance_points
                    and momentum <= -thresholds.minimum_momentum_points):
                return result(
                    TurningState.TREND_CONTINUATION, watched_level,
                    "Price accepted below support; do not fade the move",
                )
        if abs(price - level_price) <= thresholds.level_proximity_points * 2:
            return result(previous_state, watched_level, "Potential setup remains unconfirmed")

    if not levels:
        return result(TurningState.WAIT, None, "WAIT: no prior-known level is available")
    nearest = min(levels, key=lambda level: abs(level.price - price))
    touching = abs(nearest.price - price) <= thresholds.level_proximity_points
    if not touching or vwap_z is None or not math.isfinite(vwap_z):
        return result(TurningState.WAIT, nearest, "WAIT: no extended level test")
    if (flow.classified_trades < thresholds.minimum_classified_trades
            or flow.classified_pct < thresholds.minimum_classified_pct):
        return result(TurningState.WAIT, nearest, "WAIT: insufficient classifiable option flow")
    if (
        price >= nearest.price
        and vwap_z >= thresholds.vwap_extension_z
        and momentum >= thresholds.minimum_momentum_points
        and flow_ratio >= thresholds.strong_flow_ratio
    ):
        return result(
            TurningState.POTENTIAL_TOP, nearest,
            "Potential top: extended rise and bullish flow reached a known level; rejection not confirmed",
        )
    if (
        price <= nearest.price
        and vwap_z <= -thresholds.vwap_extension_z
        and momentum <= -thresholds.minimum_momentum_points
        and flow_ratio <= -thresholds.strong_flow_ratio
    ):
        return result(
            TurningState.POTENTIAL_BOTTOM, nearest,
            "Potential bottom: extended decline and bearish flow reached a known level; bounce not confirmed",
        )
    return result(TurningState.WAIT, nearest, "WAIT: level, extension, momentum, and flow do not align")


def build_key_levels(
    prior_sessions: Sequence[SessionOHLC],
    *,
    session_date: date,
    session_open: float,
    reference_price: float,
    round_increment: float = 25.0,
) -> list[KeyLevel]:
    """Build only levels knowable at the start of ``session_date``."""
    previous = sorted(
        (bar for bar in prior_sessions if bar.session_date < session_date),
        key=lambda bar: bar.session_date,
    )
    by_date = {bar.session_date: bar for bar in previous}
    levels: list[KeyLevel] = [KeyLevel(session_open, "Current day open", "open")]
    prior_date = _previous_trading_day(session_date)
    last = by_date.get(prior_date)
    if last is not None:
        levels.extend((
            KeyLevel(last.open, "Previous day open", "daily"),
            KeyLevel(last.high, "Previous day high", "daily"),
            KeyLevel(last.low, "Previous day low", "daily"),
            KeyLevel(last.close, "Previous day close", "daily"),
        ))

    week_start = session_date - timedelta(days=session_date.weekday())
    known_week_dates = _trading_dates(week_start, session_date - timedelta(days=1))
    current_week_bars = [by_date[d] for d in known_week_dates if d in by_date]
    if not known_week_dates or len(current_week_bars) == len(known_week_dates):
        week_open = current_week_bars[0].open if current_week_bars else session_open
        levels.append(KeyLevel(week_open, "Current week open", "weekly"))
    prior_week_end = week_start - timedelta(days=1)
    prior_week_start = prior_week_end - timedelta(days=6)
    prior_week_dates = _trading_dates(prior_week_start, prior_week_end)
    if prior_week_dates and all(d in by_date for d in prior_week_dates):
        levels.extend(_period_levels([by_date[d] for d in prior_week_dates], "Previous week", "weekly"))

    month_start = session_date.replace(day=1)
    known_month_dates = _trading_dates(month_start, session_date - timedelta(days=1))
    current_month_bars = [by_date[d] for d in known_month_dates if d in by_date]
    if not known_month_dates or len(current_month_bars) == len(known_month_dates):
        month_open = current_month_bars[0].open if current_month_bars else session_open
        levels.append(KeyLevel(month_open, "Current month open", "monthly"))
    prior_month_end = month_start - timedelta(days=1)
    prior_month_start = prior_month_end.replace(day=1)
    prior_month_dates = _trading_dates(prior_month_start, prior_month_end)
    if prior_month_dates and all(d in by_date for d in prior_month_dates):
        levels.extend(_period_levels([by_date[d] for d in prior_month_dates], "Previous month", "monthly"))

    if round_increment <= 0:
        raise ValueError("round_increment must be positive")
    center = round(reference_price / round_increment) * round_increment
    for offset in range(-2, 3):
        price = center + offset * round_increment
        levels.append(KeyLevel(price, f"Round level {price:g}", "round"))
    return levels


def select_contract(
    snapshots: Iterable[OptionSnapshot],
    *,
    direction: str,
    spx_price: float,
    now: datetime,
    market_close: datetime,
    policy: ContractPolicy = ContractPolicy(),
) -> ContractDecision:
    """Select a liquid affordable 0DTE candidate, or explicitly wait."""
    if direction not in {"call", "put"}:
        return ContractDecision(None, "WAIT: setup not confirmed")
    if now.tzinfo is None or market_close.tzinfo is None:
        return ContractDecision(None, "WAIT: timestamps are not timezone-aware")
    if market_close - now < timedelta(minutes=policy.minimum_minutes_remaining):
        return ContractDecision(None, "WAIT: fewer than 30 minutes remain")

    session_date = now.astimezone(ET).date()
    same_day = [item for item in snapshots if item.expiration == session_date and item.right == direction]
    if not same_day:
        return ContractDecision(None, "WAIT: no 0DTE expiration")
    affordable = [item for item in same_day if 0 < item.ask <= policy.max_ask]
    if not affordable:
        return ContractDecision(None, "WAIT: no contract is within the $10.00 ask limit")

    liquid: list[OptionSnapshot] = []
    stale_seen = spread_seen = delta_seen = False
    for item in affordable:
        age = item.quote_age_seconds(now)
        if age is None or age > policy.max_quote_age_seconds:
            stale_seen = True
            continue
        if item.bid <= 0 or item.bid_size < policy.minimum_size or item.ask_size < policy.minimum_size:
            spread_seen = True
            continue
        if item.spread_pct is None or item.spread_pct > policy.max_spread_pct:
            spread_seen = True
            continue
        greek_age = _timestamp_age_seconds(item.greeks_timestamp, now)
        if greek_age is None or greek_age > policy.max_greek_age_seconds:
            delta_seen = True
            continue
        absolute_delta = abs(item.delta) if item.delta is not None else None
        if absolute_delta is None or not policy.min_abs_delta <= absolute_delta <= policy.max_abs_delta:
            delta_seen = True
            continue
        liquid.append(item)

    if not liquid:
        if stale_seen:
            return ContractDecision(None, "WAIT: qualifying quotes are stale")
        if spread_seen:
            return ContractDecision(None, "WAIT: qualifying spreads are too wide or illiquid")
        if delta_seen:
            return ContractDecision(None, "WAIT: only low/high-delta contracts fit the budget")
        return ContractDecision(None, "WAIT: no contract passed liquidity checks")

    chosen = min(liquid, key=lambda item: (
        abs(abs(item.delta or 0.0) - policy.target_abs_delta),
        item.spread_pct or float("inf"),
        abs(item.strike - spx_price),
        -(item.bid_size + item.ask_size),
    ))
    return ContractDecision(chosen, "Candidate satisfies the preregistered shadow policy")


def _timestamp_age_seconds(stamp: datetime | None, now: datetime) -> float | None:
    if stamp is None or stamp.tzinfo is None:
        return None
    age = (now.astimezone(stamp.tzinfo) - stamp).total_seconds()
    return age if age >= 0 else None


def _previous_trading_day(session_date: date) -> date:
    candidate = session_date - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _trading_dates(start: date, end: date) -> list[date]:
    if end < start:
        return []
    dates: list[date] = []
    current = start
    while current <= end:
        if is_trading_day(current):
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _period_levels(bars: Sequence[SessionOHLC], label: str, kind: str) -> list[KeyLevel]:
    ordered = sorted(bars, key=lambda bar: bar.session_date)
    return [
        KeyLevel(ordered[0].open, f"{label} open", kind),
        KeyLevel(max(bar.high for bar in ordered), f"{label} high", kind),
        KeyLevel(min(bar.low for bar in ordered), f"{label} low", kind),
        KeyLevel(ordered[-1].close, f"{label} close", kind),
    ]
