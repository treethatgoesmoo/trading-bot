# Explicit research and historical evidence readers

This package is **not the live bot**. Nothing here installs a Discord cog or schedules a job. The old forecast/swing/context/ORB implementations remain only where a selected replay or historical report needs them. Retired notification cogs, views, watchers, contract picker, economic-calendar integration and automatic Git housekeeping were deleted, not hidden behind a legacy mode.

## Setup and entry points

From the repository root, on the supported Python 3.12+ environment:

```bash
python -m pip install -r requirements-research.txt
python -m research.report_theta_audit --help
python -m research.backtest_momentum_extension --help
python -m research.collect_spxw_research --help
```

The former root scripts moved here. **Use `python -m research.<name>`**, not `python research/<name>.py`; the latter does not reliably resolve the shared root packages. Update external cron jobs and personal shell aliases explicitly. There are no root compatibility wrappers.

| Group | Entry points | Side effects / limits |
|---|---|---|
| Local historical reporting | `report_theta_audit`, `analyze_session_thirds`, `backtest_extension_score` | Read historical records. Audit report is request/context/ORB reporting, **not current market-state performance**. |
| Historical dashboard | `dashboard` | Writes root `dashboard.html`; `--open` opens a browser. Not a current state dashboard. |
| Historical Yahoo replay | `backtest_momentum_extension`, `backtest_open_close_momentum`, `backtest_timeframe_context`, `backtest_filters`, `backtest_patterns`, `backtest_swing`, `analyze_days` | May fetch market data and write snapshots/output. Results are historical hypotheses, not live signals. |
| Historical backfill | `backfill` | Fetches data and can write `data/`. Inspect usage first; `--bias-report` is its non-writing bias report. |
| Manual weight tuning | `tune_weights`, `tune_swing_weights` | Reads historical results; explicit `--write` changes historical weights. Those weights do not configure the state engine. |
| Licensed research collection | `collect_spxw_research` | Explicit paid-provider SPX/SPXW requests, optional Yahoo SPY, local licensed evidence writes. Not a daemon. Verify subscriptions/terms before use. |

Most tools have argparse `--help`; positional-only tools document usage in their module docstrings. Do not assume importing a tool or asking it for an unsupported argument is a safe way to discover behavior. No data collection is required to run the regression tests.

## Boundaries

- `research/config.py` contains only settings referenced by retained research. It does not import live `config.py` or contain Discord channels/notification flags/Git schedules. Shared `core.thetadata_client` and `core.theta_usage` still use live credentials and the original bounded telemetry configuration, because they are shared adapters—not research strategy settings.
- `research/core/` contains historical analytics, tracking, tuning, Yahoo/provider-fallback infrastructure and context reporting.
- `research/zero_dte/` contains historical forecast replay, frozen ORB/context/action interpretation and the pure experimental flow model. This is not a second runnable bot.
- `research/swing/strategy.py` is the historical daily replay model. No swing cog, open-position scheduler or option-contract picker remains.
- `core/paths.py` still anchors paths to the repository root. Moving code does not relocate, rename or reset evidence. `data/`, `.thetadata-data/`, existing frozen snapshots and generated `dashboard.html` were not modified by retirement.
- Original ORB/action rules are preserved for historical interpretation. Their outcomes must not be attributed to the new market-state classifier. New production code must not import these modules, even for timestamp helpers; use `core.session_bars` instead.
- No `capture_flow` adapter was retained: it only copied a now-deleted Discord flow-cog cache and has no selected offline caller. Pure `flow_filter` and turning-point/model tests remain where needed to interpret old evidence.

## Validation

```bash
python -m pip install -r requirements-dev.txt -r requirements-research.txt
python -m pytest -q tests/historical tests/test_research_boundary.py
# Entire repository, including independently implemented new state-engine tests:
python -m pytest -q
```

Historical tests live under `tests/historical/`. They cover frozen rule semantics, causality, conservative outcome grading, historical report integrity and collector write-location safeguards using synthetic inputs/mocks. Old notification/cog/UI tests were removed with their implementations. Passing historical tests does not enable those features or establish a trading edge.

The core service can be checked with `make test` without installing research-only packages; `make test-all` includes this package's tests. Actual production assembly/configuration and a live Pi check are separate integration gates.
