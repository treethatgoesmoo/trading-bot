"""
Backtest-only prototype: does an "extension / reversal-risk" score built from
already-stored forecast fields (RSI distance from 50, price distance from
VWAP, pivot-based RSI divergence) actually flag reversal zones — i.e. spots
where price goes the OPPOSITE way of the ensemble's own directional read —
more often than baseline?

This is a diagnostic, not a live signal: it reuses data/results-*.jsonl rows
that are already graded (actual next-window move already recorded honestly
by the live grader, no lookahead introduced here) — no new fetches, no
network, nothing wired into the live bot. Read-only over history, per the
build-before-ship house rule in CLAUDE.md / docs/strategy-log.md.

Definitions:
  naive_call   = the ensemble's own directional read for that window
                 (direction if BULLISH/BEARISH, else `lean` when VOLATILE;
                 rows with NEUTRAL and no lean are skipped — no directional
                 claim to test reversal against).
  outcome      = CONTINUATION  (actual == naive_call)
                 REVERSAL      (actual is directional and != naive_call)
                 FLAT          (actual == "FLAT", i.e. inside the deadband)

  extension_score (0-100), from fields already on every stored row:
    - rsi_component:   |rsi_30m - 50| / 50 * 100        (0 = neutral RSI, 100 = 0 or 100 RSI)
    - vwap_component:  |price - vwap| / vwap * 100, rescaled to 0-100 by
                        percentile rank within the sample (no ATR stored per
                        row to normalize absolute $ distance, so this uses a
                        relative rank instead — flagged as a known limitation
                        below, not hidden)
    - divergence_bonus: +40 if a pivot RSI divergence fired against the
                        naive call's direction that window (a divergence is
                        *already* a reversal claim, so this checks whether it
                        behaves like one)
    extension_score = clip(0.5*rsi_component + 0.5*vwap_component_pctile*100
                            + divergence_bonus, 0, 100)

Usage:
    python -m research.backtest_extension_score                # all graded history
    python -m research.backtest_extension_score --stream 15     # historical 15m stream
    python -m research.backtest_extension_score --days 20
"""

import argparse
from collections import defaultdict

from research.zero_dte import tracker

DIRECTIONAL = ("BULLISH", "BEARISH")


def pct(c: int, n: int) -> float:
    return round(c / n * 100, 1) if n else 0.0


def load_all_results(days: int | None) -> list[dict]:
    dates = tracker.available_dates("results")
    if days is not None and days > 0:
        dates = dates[-days:]
    out = []
    for d in dates:
        for r in tracker.load_results(d):
            r["date"] = d
            out.append(r)
    return out


def naive_call(r: dict) -> str | None:
    d = r.get("direction")
    if d in DIRECTIONAL:
        return d
    if d == "VOLATILE" and r.get("lean") in DIRECTIONAL:
        return r["lean"]
    return None  # NEUTRAL or VOLATILE with no lean — no directional claim


def outcome(r: dict, call: str) -> str | None:
    actual = r.get("actual")
    if actual == "FLAT":
        return "FLAT"
    if actual in DIRECTIONAL:
        return "CONTINUATION" if actual == call else "REVERSAL"
    return None  # ungraded


def rsi_component(r: dict) -> float | None:
    rsi = r.get("rsi_30m")
    if rsi is None:
        return None
    return abs(rsi - 50) / 50 * 100


def vwap_raw_pct(r: dict) -> float | None:
    price, vwap = r.get("price"), r.get("vwap")
    if not price or not vwap:
        return None
    return abs(price - vwap) / vwap * 100


def divergence_against_call(r: dict, call: str) -> bool:
    div = r.get("divergence")
    if div == "BULLISH_DIV" and call == "BEARISH":
        return True
    if div == "BEARISH_DIV" and call == "BULLISH":
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--stream", type=str, default=None, help="restrict to one stream, e.g. 15 or 5")
    args = ap.parse_args()

    rows = load_all_results(args.days)
    if args.stream is not None:
        want = int(args.stream)
        rows = [r for r in rows if r.get("interval") == want]

    # Build scoring inputs first (need the full sample to percentile-rank VWAP distance)
    scored = []
    vwap_pcts = []
    for r in rows:
        call = naive_call(r)
        if call is None:
            continue
        out = outcome(r, call)
        if out is None:
            continue
        rc = rsi_component(r)
        vp = vwap_raw_pct(r)
        if rc is None or vp is None:
            continue
        scored.append({"r": r, "call": call, "outcome": out, "rsi_c": rc, "vwap_raw": vp})
        vwap_pcts.append(vp)

    if not scored:
        print("No scoreable rows — check --stream/--days filters.")
        return

    vwap_sorted = sorted(vwap_pcts)
    n_v = len(vwap_sorted)

    def vwap_percentile_rank(x: float) -> float:
        # fraction of sample at or below x, *100
        import bisect
        i = bisect.bisect_right(vwap_sorted, x)
        return i / n_v * 100

    for s in scored:
        div_bonus = 40 if divergence_against_call(s["r"], s["call"]) else 0
        vwap_c = vwap_percentile_rank(s["vwap_raw"])
        s["extension_score"] = min(100.0, 0.5 * s["rsi_c"] + 0.5 * vwap_c + div_bonus)

    n = len(scored)
    base_reversal = pct(sum(1 for s in scored if s["outcome"] == "REVERSAL"), n)
    base_continuation = pct(sum(1 for s in scored if s["outcome"] == "CONTINUATION"), n)
    base_flat = pct(sum(1 for s in scored if s["outcome"] == "FLAT"), n)

    print(f"\n{'='*72}")
    print(f"  Extension/reversal-risk score backtest — n={n} directional-call rows"
          f"{' (stream ' + args.stream + 'm)' if args.stream else ' (all streams)'}")
    print(f"{'='*72}")
    print(f"  Baseline (all rows): REVERSAL {base_reversal}%  CONTINUATION {base_continuation}%  FLAT {base_flat}%\n")

    # Tertiles by extension_score
    es_sorted = sorted(s["extension_score"] for s in scored)
    t1 = es_sorted[n // 3]
    t2 = es_sorted[2 * n // 3]
    buckets = [("Low  (score < %.0f)" % t1, lambda s: s["extension_score"] < t1),
               ("Mid  (%.0f-%.0f)" % (t1, t2), lambda s: t1 <= s["extension_score"] < t2),
               ("High (score >= %.0f)" % t2, lambda s: s["extension_score"] >= t2)]

    hdr = f"  {'Extension tertile':<24}{'n':>6}{'REVERSAL %':>13}{'CONTIN %':>11}{'FLAT %':>9}{'rev edge (pp)':>15}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for label, pred in buckets:
        sub = [s for s in scored if pred(s)]
        m = len(sub)
        rev = pct(sum(1 for s in sub if s["outcome"] == "REVERSAL"), m)
        con = pct(sum(1 for s in sub if s["outcome"] == "CONTINUATION"), m)
        flat = pct(sum(1 for s in sub if s["outcome"] == "FLAT"), m)
        edge = round(rev - base_reversal, 1)
        print(f"  {label:<24}{m:>6}{rev:>13}{con:>11}{flat:>9}{edge:>+15.1f}")

    # Divergence-only cross-check (independent of RSI/VWAP components)
    div_rows = [s for s in scored if divergence_against_call(s["r"], s["call"])]
    no_div_rows = [s for s in scored if not divergence_against_call(s["r"], s["call"])]
    print(f"\n  Divergence-against-call cross-check (independent of RSI/VWAP):")
    for label, sub in [("Divergence fired against the call", div_rows),
                        ("No divergence", no_div_rows)]:
        m = len(sub)
        if m == 0:
            print(f"    {label:<38} n=0")
            continue
        rev = pct(sum(1 for s in sub if s["outcome"] == "REVERSAL"), m)
        print(f"    {label:<38} n={m:<6} REVERSAL {rev}%  (edge {rev - base_reversal:+.1f}pp)")

    # RSI-only and VWAP-only tertiles, to see which raw component (if either)
    # is doing the work once they're not blended together.
    for comp_name, comp_key in [("RSI-distance-from-50 only", "rsi_c"), ("VWAP-distance only (raw %)", "vwap_raw")]:
        vals = sorted(s[comp_key] for s in scored)
        c1 = vals[n // 3]
        c2 = vals[2 * n // 3]
        print(f"\n  {comp_name} tertiles:")
        for label, pred in [("Low", lambda s, c1=c1: s[comp_key] < c1),
                             ("Mid", lambda s, c1=c1, c2=c2: c1 <= s[comp_key] < c2),
                             ("High", lambda s, c2=c2: s[comp_key] >= c2)]:
            sub = [s for s in scored if pred(s)]
            m = len(sub)
            rev = pct(sum(1 for s in sub if s["outcome"] == "REVERSAL"), m)
            print(f"    {label:<6} n={m:<6} REVERSAL {rev}%  (edge {rev - base_reversal:+.1f}pp)")

    print(f"\n{'='*72}")
    print("  Read: if REVERSAL% rises monotonically Low->Mid->High, the score is")
    print("  finding real reversal zones. If it's flat/noisy, the blend (or its")
    print("  components) isn't adding information over the baseline reversal rate.")
    print("  KNOWN LIMITATION: vwap_component is percentile-ranked within THIS")
    print("  sample, not ATR-normalized (no per-row ATR stored) — a true version")
    print("  needs raw candle fetches (see analyze_days.py's yfinance pattern) to")
    print("  compute $ distance in ATR units and a real velocity/momentum axis")
    print("  (rate-of-change, MACD histogram slope) — categorical vote fields")
    print("  alone can't produce that; this script only tests the reversal axis.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
