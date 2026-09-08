# Trading Bot

Personal Discord decision support for manual SPX 0DTE trading. It describes and grades underlying observations; it **never places orders**.

## Current default: unified SPX context

One notification contains **Action, Structure, Setup, Momentum, Confirmation, and Invalidation**. Action is a frozen experimental price-only checklist: consider calls/puts only on aligned nonfading retests; otherwise wait or no trade. It requires manual contract checks, does not select options, and has no demonstrated edge. The audit separately groups delivered action candidates; waits are not scored as trades. It uses completed SPX minute bars, an opening-range map, prior-session reference levels, descriptive five-minute momentum, optional indicative SPY confirmation, and experimental sampled SPXW flow.

`NOTIFICATION_MODE=spx` replaces all old automatic trading notifications—even if the Pi's existing `.env` still enables forecasts, Bollinger/volume watchers, swing signals, or the full flow shadow. Old automatic economic briefings, restart notices, and retune chatter are also suppressed. History auto-commit remains quiet. Legacy code/history remain available through an explicit `NOTIFICATION_MODE=legacy` rollback; they are not running alongside the replacement.

**Status: implemented for review; not verified on the Pi.** These changes are on `feat/spx-context-actions-audit`; a Pi pull of `master` cannot install them until they reach remote `master`. Implementation tests are not evidence of strategy edge.

## Pi migration

The owner already has the repository cloned and deploys by pulling `master`, then running `make restart`. **Preserve the existing ignored `.env`; do not copy `.env.example` over it.**

Before restarting the replacement, retain the real `DISCORD_TOKEN` and `THETADATA_API_KEY`, and configure:

```env
NOTIFICATION_MODE=spx
SPX_CONTEXT_CHANNEL_ID=123456789012345678
THETA_AUDIT_START_DATE=YYYY-MM-DD
THETA_AUDIT_DAYS=30
SPX_CONTEXT_FLOW_ENABLED=1
SPX_CONTEXT_PROXY_ENABLED=1
```

Replace the example channel ID with a **verified private owner channel** and the date with the fixed deployment start date in ET. `SPX_CONTEXT_CHANNEL_ID` defaults to `0`: no new notifications and **no fallback to `ALERT_CHANNEL_ID`**. A missing key does not revive old alerts. Channel configuration alone does not verify permissions or data-license compliance.

After the changes reach remote `master`, pull it on the Pi, update the existing environment as above, then:

```bash
make restart
make status
make tail
```

See [the migration checklist and notification contract](docs/spx-notifications.md) before deployment. Verify both successful collection **and an actual delivered message** during market hours; startup logs alone are insufficient.

## Commands and subscription audit

- `!spx` / `!status`: fresh cached context, logged and graded as an on-demand delivery; no new provider requests. Expired context is unavailable, not an old directional call presented as current.
- `!audit`: private-channel offline report of requests fetched, data incorporated, notifications delivered, and paper outcomes.
- `!events`: optional on-demand economic calendar; not an automatic briefing.

```bash
python report_theta_audit.py --start YYYY-MM-DD --days 30
```

The same SPX collector supplies both notifications and the unchanged frozen ORB experiment—no duplicate current-session audit fetch. Live index collection continues after audit expiry. Optional options sampling, request telemetry, and audit mirrors stop at expiry; **billing does not**. `THETA_AUDIT_FLOW_ENABLED` is a legacy-mode setting and does not control replacement-mode flow.

Read [the audit protocol and renewal gates](docs/research/theta-subscription-audit.md). Data incorporation is not proof of usefulness; overlapping 15/30-minute SPX observations are not independent trades or option P&L.

## Fresh installation and validation

Python 3.12+ is required for the official ThetaData v3 client; no Theta Terminal/Java is needed. A fresh installation can create a virtual environment, run `pip install -r requirements.txt`, and copy `.env.example` **only when no existing `.env` needs preserving**. Configure the private channel and credentials before starting with `python main.py`.

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
python -c "import main, bot.core, zero_dte.spx_context_cog, report_theta_audit"
```

Run relevant backtests before strategy changes. There is still no honest replay harness for the retired intraday ensemble/break-and-retest system. Research commands and rejected hypotheses remain in `CLAUDE.md` and [the strategy log](docs/strategy-log.md).

## Data boundaries

- Never hand-edit/delete committed `data/` history.
- `.research-data/` contains ignored frozen Yahoo inputs.
- `.thetadata-data/context/` contains licensed local context evidence and delivery/outcome ledgers; `.thetadata-data/audit/` contains bounded audit mirrors and request metadata.
- Keep licensed records private and out of Git, tracked history, and Discord attachments. Notifications expose derived context, not raw OPRA rows. Private backup/retention is the owner's responsibility.
- Stored timestamps are UTC ISO; user-facing times are ET.

[Current architecture](docs/state-of-the-code.md) · [Notification guide](docs/spx-notifications.md) · [Research evidence](docs/strategy-log.md)
