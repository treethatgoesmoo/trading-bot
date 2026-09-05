# Next Steps

This plan starts from the repository state documented on 2026-09-04. The immediate goal is to learn whether the experimental SPXW shadow is operationally reliable and adds information—not to promote it into a trading recommendation.

## Phase 1 — Deploy the paper shadow safely

**Goal:** verify one complete market-hours run without changing any strategy thresholds.

On the Raspberry Pi:

```bash
git switch master
git pull origin master
python -m unittest discover -s tests -q
python -m core.thetadata_client --symbol SPXW
```

Keep the existing secret and add or confirm:

```env
THETADATA_API_KEY=...
SPX_FLOW_ENABLED=1
SPX_FLOW_ALERT_CHANNEL_ID=...
SPX_FLOW_POLL_SECONDS=60
SPX_FLOW_LOOKBACK_MINUTES=5
SPX_FLOW_STRIKE_RANGE=2
SPX_FLOW_ALERT_COOLDOWN_MINUTES=10
```

Then restart the bot using the normal Pi procedure. Do not send the API key to Discord, GitHub, logs, or an assistant.

During the session:

- Confirm the ordinary SPY forecast and swing cogs still operate normally.
- Run `!spxflow` after the monitor has warmed up. It should display a cached observation without changing monitor state.
- Treat every SPXW result as experimental/paper-only.
- Do not alter the flow, level, momentum, or extension thresholds in response to individual alerts.
- Watch for ThetaData duration warnings, repeated provider errors, circuit-breaker pauses, Discord duplicates, or event-loop delays.

After the close, confirm `.thetadata-data/shadow/` contains the day's derived observation and alert files and that no `.thetadata-data` file appears in `git status`.

### Operational acceptance checklist

Complete at least three full sessions before calling the live plumbing stable:

- [ ] No impact on existing forecast or swing scheduling
- [ ] No duplicate Discord transitions
- [ ] No raw OPRA rows in Git, Discord, or normal logs
- [ ] No permanently hung ThetaData request
- [ ] Request durations recorded and compatible with the polling interval
- [ ] Circuit breaker recovers after temporary failures
- [ ] Every posted alert gets a 15-minute SPX outcome
- [ ] Every sufficiently early alert gets a 30-minute SPX outcome
- [ ] Every displayed contract has an ask entry and either a valid bid exit or an explicit missing result
- [ ] Restarting the bot does not lose or duplicate pending outcomes

A failure here is an engineering issue, not a strategy result. Fix it without tuning the hypothesis.

## Phase 2 — Measure data quality before interpreting flow

**Goal:** determine whether the conservative OPRA subset is usable.

For each session, summarize:

- total, unique, and duplicate SPXW prints;
- print counts by OPRA condition code;
- condition-code `0` share;
- valid preceding-NBBO share;
- ask-side, bid-side, midpoint, crossed, stale, and invalid classifications;
- call and put premium by aggressor class;
- classifiable share by time of day and strike distance;
- concentration of directional premium in the largest prints and expensive ITM contracts;
- ThetaData request count, duration distribution, failures, and retries; and
- quote/Greek freshness and strike coverage for paper candidates.

Add a local analysis command that reads `.thetadata-data` and emits only aggregate, non-licensed results. Do not copy raw rows into tracked `data/`.

The current directional-flow ratio must remain descriptive until this profile shows that it is not dominated by a few contracts, stale quotes, or a narrow time window. Consider delta-adjusted notional only as a separately preregistered variant; do not silently replace the existing definition.

## Phase 3 — Collect complete frozen sessions

**Goal:** build a reproducible local research set containing all required SPX, SPY, and SPXW inputs.

After each session closes:

```bash
python collect_spxw_research.py --date YYYY-MM-DD --strike-range 5
```

Use the normal collection with SPY included. The earlier `--skip-spy --strike-range 2` run was a plumbing test and cannot evaluate the full VWAP/volume context.

Collection rules:

- Run only after the calendar-aware close; the collector rejects incomplete sessions.
- Never use `--refresh` unless deliberately replacing a documented bad snapshot.
- Preserve metadata and SHA-256 sidecars with every snapshot.
- Check disk use before scaling collection; OPRA trade files are large.
- Establish a licensed-data-compliant local backup and retention policy.
- Do not auto-commit or redistribute the snapshots.

Before bulk backfill, sample several dates—including an early close, high-volatility day, low-volume day, and ordinary day—to measure size, runtime, endpoint limits, and completeness.

## Phase 4 — Build deterministic replay and shadow reporting

Two tools are still needed.

### 4.1 Shadow-results report

Add `analyze_spxw_shadow.py` to report, by state and horizon:

- alert count and unique session count;
- SPX directional accuracy and average/median movement;
- paper ask-to-bid return, win rate, median return, and drawdown proxy;
- missing-entry and missing-exit rates;
- results by time of day, level type, spread, delta, and flow-quality bucket; and
- session-clustered uncertainty rather than treating alerts from one day as independent.

The report must include all observations and clearly separate potential states, rejection/bounce states, and trend continuation. It must never discard losing or missing outcomes silently.

### 4.2 Frozen historical replay

Add `backtest_spxw_turning_points.py` that imports the same pure rules used live and advances through each session chronologically. It must enforce:

- only prior-known levels and data;
- exact event timestamps with no future bars or quotes;
- session boundaries and NYSE early closes;
- the same episode expiry, contact, rejection/bounce, and continuation logic;
- the same OPRA condition and NBBO-quality filters;
- no threshold selection using holdout results; and
- exact ask-entry/bid-exit grading where the historical data supports the live contract policy.

Historical contract selection may require adding historical Greek retrieval. If the Standard data cannot reproduce signal-time delta, either add the entitled endpoint or preregister and label a separate moneyness-based research policy. Do not pretend snapshot Greeks were historically available.

## Phase 5 — Compare against baselines

The turning-point system must add value beyond price geometry alone. Compare it with:

1. every eligible prior-level touch;
2. the same price/VWAP/momentum rule without option flow;
3. an unconditional fade at extreme extension;
4. continuation after acceptance through the level;
5. always-call, always-put, and same-time random-direction controls; and
6. contract selection without the turning-point filter.

Report both underlying direction and executable paper option results. An SPX directional win is not automatically an SPXW profit after spread, theta, quote latency, and contract selection.

Also test whether option flow improves the price-only model out of sample. A good standalone flow accuracy number is insufficient if it adds no incremental information.

## Phase 6 — Freeze the evaluation design

Before inspecting final holdout results, write down:

- training dates;
- validation dates;
- one untouched chronological holdout;
- primary state and horizon;
- primary outcome metric;
- baseline and required improvement;
- minimum independent session and alert counts;
- acceptable missing-exit rate;
- maximum spread and drawdown policy; and
- exact promotion, revision, and rejection rules.

Tune only on training sessions. Use validation sessions for one documented model choice, then evaluate the holdout once. Confidence intervals must be clustered by session.

Prospective Discord shadow data must remain a separately reported test set rather than being folded back into training whenever results disappoint.

## Phase 7 — Decision gate

### Keep paper-only

Keep collecting without promotion if the system is operationally sound but the sample is small or uncertainty still spans no edge.

### Revise and retest

Revise only when a documented failure mode is visible in training data—for example poor condition coverage or level construction. Record the change in `docs/strategy-log.md`, version the grading/state schema, and restart evaluation with a new untouched period.

### Reject

Disable the alert stream if it fails to improve on price-only baselines, produces negative executable expectancy, has unacceptable missing quotes/spreads, or depends on unstable condition filtering. Preserve the historical record and rejection rationale.

### Consider broader use

Do not remove experimental wording, add SPXW flow to the SPY forecast voter, or describe contracts as recommendations unless both frozen chronological holdout and prospective ask-to-bid results show useful incremental edge, adequate independent sample size, operational reliability, and acceptable drawdown.

No result authorizes automatic order execution. Any future execution feature would require a separate design, risk controls, broker integration review, and explicit owner decision.

## Recommended order of work

1. Merge and deploy the current documentation/code from `master`.
2. Run three controlled full-session paper-shadow observations.
3. Add the aggregate data-quality and shadow-results report.
4. Collect complete SPY-inclusive frozen sessions while monitoring storage.
5. Build deterministic historical replay using the live pure rules.
6. Preregister the chronological evaluation and promotion thresholds.
7. Run baseline, incremental-value, and executable-outcome comparisons.
8. Keep, revise, or reject the feature based on recorded evidence.
