"""Real-time RSI reversal watcher.

Runs on the bot's poll loop and pings the moment it spots a potential reversal,
using three techniques (per the classic "how to actually use RSI" playbook):

  1. Threshold crossings — RSI entering/leaving overbought/oversold, tiered:
     a 70/30 "warning" and a stronger 80/20 "extreme". Leaving a zone is treated
     as the actual reversal *trigger*; entering it is a *watch*.
  2. Divergence — price makes a higher high while RSI makes a lower high (bearish),
     or price lower low while RSI higher low (bullish). The strongest reversal hint.
  3. Failure swings — Wilder's RSI-only structure break.

Watches 5-min and 15-min in parallel; when both agree on direction in the same
cycle the alert is flagged CONFIRMED (higher conviction).

De-duplication: each condition fires once per occurrence — a threshold alert only
on the band transition, divergence/failure-swing keyed by the pivots involved — so
it pings on the *event*, not every poll while RSI stays elevated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config as cfg

TIMEFRAMES = ("5", "15")
TF_LABEL = {"5": "5m", "15": "15m"}

# Band ranks for detecting threshold transitions.
_RANK = {"extreme_os": 0, "os": 1, "mid": 2, "ob": 3, "extreme_ob": 4}


def rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI as a full series (same math as strategy._compute_rsi)."""
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


@dataclass
class RSIAlert:
    ticker: str
    timeframe: str               # "5" | "15"
    kind: str                    # THRESHOLD | DIVERGENCE | FAILURE_SWING
    direction: str               # BULLISH | BEARISH (implied reversal direction)
    stage: str                   # watch | reversal
    rsi: float
    message: str
    tier: str | None = None      # warning | extreme (threshold only)
    confirmed: bool = False      # both timeframes agree this cycle


@dataclass
class _TFState:
    band: str | None = None
    # Dedup keyed by the *bar timestamp* of the pivot (stable as the window
    # slides), per direction — so a given divergence/failure-swing pings once.
    last_bear_div_ts: object = None
    last_bull_div_ts: object = None
    last_bear_fs_ts: object = None
    last_bull_fs_ts: object = None
    primed: bool = False         # seen at least one bar (avoids startup ping)


def _band(rsi: float, ob: float, ob_x: float, os: float, os_x: float) -> str:
    if rsi >= ob_x:
        return "extreme_ob"
    if rsi >= ob:
        return "ob"
    if rsi <= os_x:
        return "extreme_os"
    if rsi <= os:
        return "os"
    return "mid"


def _pivots(vals: np.ndarray, order: int) -> tuple[list[int], list[int]]:
    """Indices of local maxima and minima (strict over a +/- order window)."""
    highs, lows = [], []
    n = len(vals)
    for i in range(order, n - order):
        w = vals[i - order:i + order + 1]
        if vals[i] == w.max() and vals[i] > vals[i - 1]:
            highs.append(i)
        if vals[i] == w.min() and vals[i] < vals[i - 1]:
            lows.append(i)
    return highs, lows


class RSIWatcher:
    """Per-ticker RSI watcher across 5m and 15m timeframes."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.period = cfg.RSI_PERIOD
        self.ob, self.ob_x = cfg.RSI_OB_WARN, cfg.RSI_OB_EXTREME
        self.os, self.os_x = cfg.RSI_OS_WARN, cfg.RSI_OS_EXTREME
        self.div_lookback = cfg.RSI_DIV_LOOKBACK
        self.order = cfg.RSI_PIVOT_ORDER
        self.div_directions = cfg.RSI_DIVERGENCE_DIRECTIONS  # both | bullish | bearish | off
        self._state: dict[str, _TFState] = {tf: _TFState() for tf in TIMEFRAMES}

    def _div_enabled(self, direction: str) -> bool:
        mode = (self.div_directions or "both").lower()
        if mode == "off":
            return False
        if mode == "both":
            return True
        return mode == direction.lower()

    def reset(self) -> None:
        self._state = {tf: _TFState() for tf in TIMEFRAMES}

    # ── Detectors (pure; operate on a candle DataFrame) ──────────────────

    def evaluate(self, timeframe: str, df: pd.DataFrame) -> list[RSIAlert]:
        if df is None or df.empty or len(df) < self.period + 2:
            return []
        closes = df["close"].astype(float).reset_index(drop=True)
        rsi = rsi_series(closes, self.period)
        if rsi.isna().all():
            return []
        cur = rsi.iloc[-1]
        if np.isnan(cur):
            return []
        times = (df["timestamp"].reset_index(drop=True)
                 if "timestamp" in df.columns else pd.Series(range(len(closes))))
        st = self._state[timeframe]
        alerts: list[RSIAlert] = []

        alerts += self._threshold(timeframe, st, cur)
        alerts += self._divergence(timeframe, st, closes, rsi, times, cur)
        alerts += self._failure_swing(timeframe, st, rsi, times, cur)
        return alerts

    def _threshold(self, tf: str, st: _TFState, cur: float) -> list[RSIAlert]:
        new_band = _band(cur, self.ob, self.ob_x, self.os, self.os_x)
        old_band = st.band
        st.band = new_band
        if not st.primed:
            st.primed = True
            return []  # establish baseline silently — don't ping on startup
        if old_band is None or new_band == old_band:
            return []

        rising = _RANK[new_band] > _RANK[old_band]
        out = []

        def mk(direction, stage, tier, text):
            out.append(RSIAlert(self.ticker, tf, "THRESHOLD", direction, stage,
                                 round(cur, 1), text, tier=tier))

        if rising:
            if _RANK[new_band] >= _RANK["ob"]:  # entered overbought zone
                tier = "extreme" if new_band == "extreme_ob" else "warning"
                lvl = self.ob_x if tier == "extreme" else self.ob
                mk("BEARISH", "watch", tier, f"RSI {cur:.0f} entered overbought (≥{lvl:.0f}) — bearish reversal watch")
            elif _RANK[old_band] <= _RANK["os"] and _RANK[new_band] >= _RANK["mid"]:
                mk("BULLISH", "reversal", None, f"RSI {cur:.0f} rose back out of oversold — bullish reversal trigger")
        else:  # falling
            if _RANK[new_band] <= _RANK["os"]:  # entered oversold zone
                tier = "extreme" if new_band == "extreme_os" else "warning"
                lvl = self.os_x if tier == "extreme" else self.os
                mk("BULLISH", "watch", tier, f"RSI {cur:.0f} entered oversold (≤{lvl:.0f}) — bullish reversal watch")
            elif _RANK[old_band] >= _RANK["ob"] and _RANK[new_band] <= _RANK["mid"]:
                mk("BEARISH", "reversal", None, f"RSI {cur:.0f} fell back out of overbought — bearish reversal trigger")
        return out

    def _divergence(self, tf: str, st: _TFState, closes: pd.Series,
                    rsi: pd.Series, times: pd.Series, cur: float) -> list[RSIAlert]:
        window = min(self.div_lookback, len(closes))
        offset = len(closes) - window
        c = closes.iloc[-window:].to_numpy()
        r = rsi.iloc[-window:].to_numpy()
        if np.isnan(r).all():
            return []
        p_highs, p_lows = _pivots(c, self.order)

        # Bearish: last two price swing highs rising, RSI at those highs falling.
        if self._div_enabled("bearish") and len(p_highs) >= 2:
            a, b = p_highs[-2], p_highs[-1]
            if c[b] > c[a] and r[b] < r[a] and not np.isnan(r[b]):
                ts = times.iloc[offset + b]
                if st.last_bear_div_ts != ts:
                    st.last_bear_div_ts = ts
                    return [RSIAlert(self.ticker, tf, "DIVERGENCE", "BEARISH", "reversal",
                                     round(cur, 1),
                                     f"Bearish divergence — price higher high, RSI lower high ({r[a]:.0f}→{r[b]:.0f})")]
        # Bullish: last two price swing lows falling, RSI at those lows rising.
        if self._div_enabled("bullish") and len(p_lows) >= 2:
            a, b = p_lows[-2], p_lows[-1]
            if c[b] < c[a] and r[b] > r[a] and not np.isnan(r[b]):
                ts = times.iloc[offset + b]
                if st.last_bull_div_ts != ts:
                    st.last_bull_div_ts = ts
                    return [RSIAlert(self.ticker, tf, "DIVERGENCE", "BULLISH", "reversal",
                                     round(cur, 1),
                                     f"Bullish divergence — price lower low, RSI higher low ({r[a]:.0f}→{r[b]:.0f})")]
        return []

    def _failure_swing(self, tf: str, st: _TFState, rsi: pd.Series,
                       times: pd.Series, cur: float) -> list[RSIAlert]:
        window = min(self.div_lookback, len(rsi))
        offset = len(rsi) - window
        r = rsi.iloc[-window:].to_numpy()
        if len(r) < 2 * self.order + 3:
            return []
        # NaNs (RSI warm-up) never register as pivots, so alignment is preserved.
        highs, lows = _pivots(r, self.order)

        # Bearish failure swing: peak1>OB, trough, lower peak2 (<peak1), now break below trough.
        if len(highs) >= 2 and lows:
            p1, p2 = highs[-2], highs[-1]
            troughs = [t for t in lows if p1 < t < p2]
            if troughs and r[p1] > self.ob and r[p2] < r[p1]:
                fail = r[troughs[-1]]
                if cur < fail:
                    ts = times.iloc[offset + troughs[-1]]
                    if st.last_bear_fs_ts != ts:
                        st.last_bear_fs_ts = ts
                        return [RSIAlert(self.ticker, tf, "FAILURE_SWING", "BEARISH", "reversal",
                                         round(cur, 1),
                                         f"Bearish failure swing — RSI broke below {fail:.0f} after a lower high")]
        # Bullish failure swing: trough1<OS, peak, higher trough2 (>trough1), now break above peak.
        if len(lows) >= 2 and highs:
            t1, t2 = lows[-2], lows[-1]
            peaks = [p for p in highs if t1 < p < t2]
            if peaks and r[t1] < self.os and r[t2] > r[t1]:
                fail = r[peaks[-1]]
                if cur > fail:
                    ts = times.iloc[offset + peaks[-1]]
                    if st.last_bull_fs_ts != ts:
                        st.last_bull_fs_ts = ts
                        return [RSIAlert(self.ticker, tf, "FAILURE_SWING", "BULLISH", "reversal",
                                         round(cur, 1),
                                         f"Bullish failure swing — RSI broke above {fail:.0f} after a higher low")]
        return []

    # ── Orchestration ────────────────────────────────────────────────────

    async def check(self, client) -> list[RSIAlert]:
        """Fetch both timeframes, evaluate, and flag cross-timeframe agreement."""
        all_alerts: list[RSIAlert] = []
        for tf in TIMEFRAMES:
            try:
                df = await client.fetch_candles(self.ticker, interval=tf, count=120)
            except Exception:
                continue
            all_alerts.extend(self.evaluate(tf, df))

        # Confirm: a direction seen on both 5m and 15m this cycle.
        dirs_by_tf = {tf: {a.direction for a in all_alerts if a.timeframe == tf} for tf in TIMEFRAMES}
        agreed = dirs_by_tf["5"] & dirs_by_tf["15"]
        if agreed:
            for a in all_alerts:
                if a.direction in agreed:
                    a.confirmed = True
        return all_alerts
