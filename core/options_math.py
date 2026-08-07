"""Black-Scholes option pricing — pure math, no network.

Used by the 0DTE option projection to reprice a quoted contract at the day
forecast's projected high/low. Kept deliberately small: European BS on a
non-dividend underlying is plenty for a same-day SPY projection whose real
error sources are the ATR range estimate and intraday IV drift, not the
pricing model.
"""

from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, rate: float,
             sigma: float, kind: str) -> float:
    """Black-Scholes price for a European call/put. At t<=0 or sigma<=0,
    collapses to intrinsic value."""
    intrinsic = max(spot - strike, 0.0) if kind == "call" else max(strike - spot, 0.0)
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return intrinsic
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if kind == "call":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t_years) * _norm_cdf(d2)
    return strike * math.exp(-rate * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t_years: float,
                rate: float, kind: str, lo: float = 0.005, hi: float = 5.0,
                tol: float = 1e-4, max_iter: int = 80) -> float | None:
    """Implied volatility from an observed price, by bisection.

    Returns None when the price sits outside the no-arbitrage band the model
    can reach (e.g. a quote below intrinsic from a stale/crossed market).
    """
    if t_years <= 0 or price <= 0:
        return None
    if bs_price(spot, strike, t_years, rate, lo, kind) > price:
        return None  # below even near-zero-vol value (sub-intrinsic quote)
    if bs_price(spot, strike, t_years, rate, hi, kind) < price:
        return None  # above max reachable value
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        val = bs_price(spot, strike, t_years, rate, mid, kind)
        if abs(val - price) < tol:
            return mid
        if val < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0
