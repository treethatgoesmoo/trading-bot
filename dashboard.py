"""Generate a self-contained HTML performance dashboard from the bot's history.

    python dashboard.py            # all recorded history
    python dashboard.py 30         # last 30 days with data
    python dashboard.py 30 --open  # ...and open it in the browser

Reads data/ via analytics.build_report() and writes dashboard.html (no external
dependencies — inline CSS, no JS required). Use it to spot which indicators are
dragging the algorithm down, whether confidence is calibrated, and how bias /
trades are doing over time.
"""

from __future__ import annotations

import html
import os
import sys
import webbrowser

import analytics

OUT_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")

# Accuracy colour thresholds (a coin-flip baseline is 50%).
GOOD, OKAY = 55.0, 45.0


def _color(acc: float | None) -> str:
    if acc is None:
        return "#555"
    if acc >= GOOD:
        return "#1f9d55"
    if acc >= OKAY:
        return "#c98a00"
    return "#d64545"


def _bar(acc: float | None, label: str | None = None) -> str:
    """A horizontal accuracy bar, 0-100%."""
    if acc is None:
        return '<span class="muted">—</span>'
    text = label if label is not None else f"{acc:.0f}%"
    return (
        f'<div class="bar"><div class="fill" style="width:{min(acc,100):.0f}%;'
        f'background:{_color(acc)}"></div><span class="bar-label">{text}</span></div>'
    )


def _card(title: str, body: str, subtitle: str = "") -> str:
    sub = f'<div class="sub">{html.escape(subtitle)}</div>' if subtitle else ""
    return f'<section class="card"><h2>{html.escape(title)}</h2>{sub}{body}</section>'


def _esc(v) -> str:
    return html.escape(str(v))


def render(report: dict) -> str:
    lo, hi = report["date_range"]
    fc = report["forecast"]
    n_days = len(report["dates"])
    range_txt = f"{lo} → {hi}" if lo else "no data yet"

    # ── Headline KPIs ────────────────────────────────────────────────────
    bias, trades = report["bias"], report["trades"]
    kpis = [
        ("Forecast accuracy", f"{fc['accuracy']:.0f}%", f"{fc['correct']}/{fc['scored']} scored", _color(fc["accuracy"])),
        ("Bias accuracy", f"{bias['accuracy']:.0f}%" if bias["scored"] else "—",
         f"{bias['correct']}/{bias['scored']} days", _color(bias["accuracy"] if bias["scored"] else None)),
        ("Trade win rate", f"{trades['win_rate']:.0f}%" if (trades['wins'] + trades['losses']) else "—",
         f"{trades['wins']}W / {trades['losses']}L", _color(trades["win_rate"] if (trades['wins'] + trades['losses']) else None)),
        ("Net P/L", f"${trades['total_pnl']:+.2f}", f"avg {trades['avg_r']:+.2f}R",
         "#1f9d55" if trades["total_pnl"] >= 0 else "#d64545"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-val" style="color:{c}">{_esc(v)}</div>'
        f'<div class="kpi-name">{_esc(name)}</div><div class="kpi-sub">{_esc(sub)}</div></div>'
        for name, v, sub, c in kpis
    )

    # ── Per-indicator accuracy (the headline diagnostic) ─────────────────
    rows = ""
    for r in report["indicators"]:
        w = f'{r["weight"]:.1f}' if r["weight"] is not None else "—"
        flag = " ⚠️" if r["accuracy"] < OKAY else ""
        rows += (
            f'<tr><td>{_esc(r["indicator"])}{flag}</td><td class="num">{r["n"]}</td>'
            f'<td class="num">{w}</td><td class="barcell">{_bar(r["accuracy"])}</td></tr>'
        )
    indicators_html = (
        '<table><thead><tr><th>Indicator</th><th class="num">Signals</th>'
        '<th class="num">Weight</th><th>Accuracy</th></tr></thead><tbody>'
        + (rows or '<tr><td colspan="4" class="muted">No evaluated forecasts yet.</td></tr>')
        + "</tbody></table>"
    )

    # ── Calibration ──────────────────────────────────────────────────────
    cal_rows = ""
    for c in report["calibration"]:
        if not c["n"]:
            cal_rows += f'<tr><td>{_esc(c["label"])}</td><td class="num">0</td><td class="muted">—</td><td class="muted">—</td></tr>'
            continue
        gap = c["actual"] - c["predicted"]
        gap_c = "#1f9d55" if abs(gap) <= 7 else "#d64545"
        cal_rows += (
            f'<tr><td>{_esc(c["label"])}</td><td class="num">{c["n"]}</td>'
            f'<td class="num">{c["predicted"]:.0f}%</td>'
            f'<td class="num" style="color:{gap_c}">{c["actual"]:.0f}%</td></tr>'
        )
    calibration_html = (
        '<table><thead><tr><th>Confidence band</th><th class="num">N</th>'
        '<th class="num">Predicted</th><th class="num">Actual</th></tr></thead><tbody>'
        + cal_rows + "</tbody></table>"
    )

    # ── Simple accuracy tables (hour / ticker / day) ─────────────────────
    def acc_table(title_key: str, rows_data: list[dict], label_head: str) -> str:
        body = ""
        for r in rows_data:
            body += (
                f'<tr><td>{_esc(r["key"])}</td><td class="num">{r["n"]}</td>'
                f'<td class="barcell">{_bar(r["accuracy"])}</td></tr>'
            )
        return (
            f'<table><thead><tr><th>{html.escape(label_head)}</th>'
            f'<th class="num">N</th><th>Accuracy</th></tr></thead><tbody>'
            + (body or '<tr><td colspan="3" class="muted">No data.</td></tr>') + "</tbody></table>"
        )

    # ── Filters ──────────────────────────────────────────────────────────
    flt = report["filters"]
    flt_rows = ""
    for r in flt["by_filter"]:
        fail_label = f"{r['fail_rate']:.0f}% fail"
        flt_rows += (
            f'<tr><td>{_esc(r["filter"])}</td><td class="num">{r["failed"]}/{r["seen"]}</td>'
            f'<td class="barcell">{_bar(r["fail_rate"], fail_label)}</td></tr>'
        )
    filters_html = (
        f'<p class="sub">{flt["blocked"]}/{flt["setups"]} setups blocked by filters '
        f'({flt["block_rate"]:.0f}%).</p>'
        '<table><thead><tr><th>Filter</th><th class="num">Failed</th>'
        '<th>Fail rate</th></tr></thead><tbody>'
        + (flt_rows or '<tr><td colspan="3" class="muted">No setups recorded yet.</td></tr>')
        + "</tbody></table>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Bot — Performance Dashboard</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0d1117; color:#e6edf3; font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  header {{ padding:24px 32px; border-bottom:1px solid #21262d; }}
  header h1 {{ margin:0 0 4px; font-size:20px; }}
  header .sub {{ color:#8b949e; }}
  .wrap {{ padding:24px 32px; max-width:1100px; margin:0 auto; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin-bottom:8px; }}
  .kpi {{ background:#161b22; border:1px solid #21262d; border-radius:10px; padding:16px; }}
  .kpi-val {{ font-size:30px; font-weight:700; }}
  .kpi-name {{ color:#c9d1d9; margin-top:2px; }}
  .kpi-sub {{ color:#8b949e; font-size:12px; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  @media (max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} }}
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:10px; padding:18px 20px; margin-top:20px; }}
  .card h2 {{ margin:0 0 12px; font-size:15px; }}
  .card .sub {{ color:#8b949e; margin:-6px 0 12px; font-size:12px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:7px 8px; border-bottom:1px solid #21262d; }}
  th {{ color:#8b949e; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .barcell {{ width:46%; }}
  .bar {{ position:relative; background:#0d1117; border:1px solid #21262d; border-radius:5px; height:20px; overflow:hidden; }}
  .fill {{ position:absolute; left:0; top:0; bottom:0; }}
  .bar-label {{ position:relative; padding-left:8px; font-size:12px; line-height:20px; font-variant-numeric:tabular-nums; }}
  .muted {{ color:#6e7681; }}
  footer {{ color:#6e7681; padding:24px 32px; font-size:12px; }}
</style></head>
<body>
<header>
  <h1>📊 Trading Bot — Performance Dashboard</h1>
  <div class="sub">{_esc(range_txt)} · {n_days} day(s) with data · {fc['total']} forecasts · {fc['neutral']} neutral · avg confidence {fc['avg_confidence']:.0f}%</div>
</header>
<div class="wrap">
  <div class="kpis">{kpi_html}</div>

  {_card("Per-indicator accuracy", indicators_html, "How often each indicator's signal matched the actual 30-min move. ⚠️ = below " + f"{OKAY:.0f}% (worse than a coin flip).")}

  <div class="grid">
    {_card("Confidence calibration", calibration_html, "Predicted vs. realized hit-rate per confidence band. Red = miscalibrated.")}
    {_card("Filter performance", filters_html, "Which filters block setups most often.")}
  </div>

  <div class="grid">
    {_card("Accuracy by time of day", acc_table("hour", report["by_hour"], "Forecast time"))}
    {_card("Accuracy by ticker", acc_table("ticker", report["by_ticker"], "Ticker"))}
  </div>

  {_card("Accuracy by day", acc_table("day", report["by_day"], "Date"))}
</div>
<footer>Generated by dashboard.py · accuracy thresholds: green ≥ {GOOD:.0f}%, amber ≥ {OKAY:.0f}%, red below.</footer>
</body></html>"""


def main() -> None:
    days = None
    do_open = "--open" in sys.argv
    for arg in sys.argv[1:]:
        if arg.isdigit():
            days = int(arg)

    report = analytics.build_report(days=days)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(render(report))

    fc = report["forecast"]
    print(f"Dashboard written to {OUT_PATH}")
    print(f"  {len(report['dates'])} day(s) · {fc['scored']} scored forecasts · {fc['accuracy']:.0f}% accuracy")
    if not report["dates"]:
        print("  (no history yet — run the bot through a trading day first)")
    if do_open:
        webbrowser.open(f"file://{OUT_PATH}")


if __name__ == "__main__":
    main()
