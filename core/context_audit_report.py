"""Read-only reporting of context delivery, consumption and forward evidence.

No strategy/provider imports or writes. Request lineage means incorporation,
not profitability. Schema-1 terminal outcomes have no finalization event time;
retrospective views therefore withhold them unless the saved snapshot itself
was available by the cutoff. Complete outcomes additionally require causal bars.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

from core import market_calendar
from core.theta_usage import _ENDPOINTS, PURPOSES

UTC = timezone.utc
SOURCES = ('spx', 'prior_spx', 'spy_proxy', 'options_flow')
PAID = {'spx': 'index', 'prior_spx': 'index', 'options_flow': 'options'}
DELIVERIES = ('sent', 'suppressed', 'failed', 'unknown', 'pending')
OUTCOMES = ('complete', 'pending', 'missing', 'invalid_entry', 'session_ended', 'unknown')
SOURCE_STATUSES = frozenset({
    'available', 'unavailable', 'fresh', 'stale', 'stale_or_missing', 'missing', 'disabled', 'failed',
    'insufficient', 'stale_or_invalid', 'complete_previous_session',
    'aligned', 'opposed', 'mixed', 'directional', 'no_cached_flow',
    'stale_or_future_flow', 'source_time_unavailable', 'stale_or_future_trade_data',
    'invalid_flow', 'insufficient_classifiable_flow',
})
IDENTITY = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Invalid aware timestamp')
    return value.astimezone(UTC)


def _identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError('Invalid identity')
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _session(day, hour, minute=0):
    return market_calendar.ET.localize(datetime(day.year, day.month, day.day, hour, minute)).astimezone(UTC)


def _days(start, end):
    for i in range((end - start).days):
        yield start + timedelta(days=i)


def _finite_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Nonfinite field')
    if isinstance(value, dict):
        for v in value.values():
            _finite_json(v)
    elif isinstance(value, list):
        for v in value:
            _finite_json(v)


def _usage_row(row):
    """Normalize both raw JSON and the caller's already validated usage rows."""
    endpoint = row['endpoint']
    if endpoint not in _ENDPOINTS or row['product'] != _ENDPOINTS[endpoint]:
        raise ValueError('Invalid endpoint product')
    kind = row['event']
    if kind not in ('start', 'finish'):
        raise ValueError('Invalid event')
    purpose = row.get('purpose')
    result = dict(request_id=_identity(row['request_id']), endpoint=endpoint,
                  product=row['product'], purpose=purpose if purpose in PURPOSES else 'unspecified',
                  event=kind, started_at=_time(row['started_at']))
    if kind == 'finish':
        finished = _time(row['finished_at'])
        rows = row.get('rows')
        if (finished < result['started_at'] or type(row['success']) is not bool
                or not _number(row['elapsed_ms']) or row['elapsed_ms'] < 0
                or rows is not None and (type(rows) is not int or rows < 0)):
            raise ValueError('Invalid completion')
        result.update(finished_at=finished, success=row['success'], rows=rows)
    return result


def _telemetry(directory, start, end, as_of, pairs, diagnostics):
    events, conflicts = {}, set()

    def accept(raw, partition=None):
        try:
            row = _usage_row(raw)
            day = row['started_at'].astimezone(market_calendar.ET).date()
            if not start <= day < end or row.get('finished_at', row['started_at']) > as_of:
                return
            if partition is not None and day != partition:
                raise ValueError('Wrong partition')
            key = row['request_id'], row['event']
            if key in events:
                if events[key] != row:
                    conflicts.add(row['request_id'])
                    diagnostics['usage_conflicts'] += 1
                else:
                    diagnostics['usage_duplicates'] += 1
            else:
                events[key] = row
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            diagnostics['usage_invalid'] += 1

    if pairs is not None:
        for begin, finish in pairs:
            if begin is not None:
                accept(begin)
            if finish is not None:
                accept(finish)
    else:
        for day in _days(start, min(end, as_of.astimezone(market_calendar.ET).date() + timedelta(days=1))):
            path = directory / f'usage-{day}.jsonl'
            if not path.exists():
                continue
            try:
                with path.open(encoding='utf-8') as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            raw = json.loads(line)
                            if not isinstance(raw, dict) or raw.get('schema') != 1:
                                raise ValueError('Wrong schema')
                            accept(raw, day)
                        except (ValueError, TypeError, RecursionError):
                            diagnostics['usage_invalid'] += 1
            except (OSError, UnicodeError):
                diagnostics['usage_unreadable'] += 1
    by_id = defaultdict(dict)
    for (identity, kind), row in events.items():
        by_id[identity][kind] = row
    completed = {}
    for identity, request in by_id.items():
        begin, finish = request.get('start'), request.get('finish')
        if begin and finish and any(begin[k] != finish[k] for k in ('started_at', 'endpoint', 'product', 'purpose')):
            conflicts.add(identity)
            diagnostics['usage_conflicts'] += 1
        if finish and identity not in conflicts:
            completed[identity] = finish
    return completed


def _entry(raw, day, as_of, snapshot_known):
    """Project onto bounded metadata; never render arbitrary saved text or IDs."""
    _finite_json(raw)
    identity = _identity(raw['id'])
    assessment = raw['assessment']
    if (assessment['schema'] != 1 or assessment['version'] != 'spx-context-v1'
            or assessment['session_date'] != day.isoformat()
            or assessment['direction'] not in ('UP', 'DOWN', 'NEUTRAL')
            or type(assessment['schema']) is not int or type(assessment['fresh']) is not bool):
        raise ValueError('Invalid assessment')
    evaluated = _time(raw['evaluated_at'])
    if _time(assessment['as_of']) != evaluated or evaluated.astimezone(market_calendar.ET).date() != day:
        raise ValueError('Inconsistent assessment availability')
    if assessment.get('bar_at') is not None:
        bar = _time(assessment['bar_at'])
        if (bar.second or bar.microsecond or bar + timedelta(minutes=1) > evaluated
                or not _session(day, 9, 30) <= bar < _session(day, *market_calendar.close_time(day))):
            raise ValueError('Invalid assessment bar')
        if assessment['fresh'] and (evaluated - bar - timedelta(minutes=1)).total_seconds() > 90:
            raise ValueError('Invalid freshness claim')
    elif assessment['fresh']:
        raise ValueError('Fresh assessment has no bar')
    if evaluated > as_of:
        return None
    delivery = raw['delivery']
    if delivery not in DELIVERIES:
        raise ValueError('Invalid delivery')
    delivered = _time(raw['delivered_at']) if raw.get('delivered_at') else None
    if delivery == 'sent' and delivered is None or delivered is not None and delivered < evaluated:
        raise ValueError('Invalid delivery time')
    if delivery != 'sent' and delivered is not None:
        raise ValueError('Unexpected delivery time')
    if delivered and delivered > as_of:
        delivery = 'pending'
    elif delivery in ('failed', 'suppressed', 'unknown') and not snapshot_known:
        # These mutable states carry no transition timestamp in schema 1.
        delivery = 'unknown'
    sources = {}
    for source in SOURCES:
        value = assessment['sources'][source]
        if type(value['used']) is not bool or not isinstance(value['status'], str):
            raise ValueError('Invalid source metadata')
        sources[source] = dict(used=value['used'], status=value['status'] if value['status'] in SOURCE_STATUSES else 'other')
    if not isinstance(raw['outcomes'], dict) or any(h not in raw['outcomes'] for h in ('15', '30')):
        raise ValueError('Invalid outcomes container')
    consumed, representative = raw.get('consumed_request_ids', {}), raw.get('request_ids', {})
    if not isinstance(consumed, dict) or not isinstance(representative, dict):
        raise ValueError('Invalid provenance')
    references = {}
    for source in PAID:
        ids = consumed.get(source)
        if ids is None:
            ids = [representative[source]] if representative.get(source) is not None else []
        if not isinstance(ids, list):
            raise ValueError('Invalid consumed IDs')
        references[source] = {_identity(v) for v in ids}
        # Explicit constituent lists (including []) are authoritative. An
        # empty latest refresh is acquisition, not returned-data consumption.
    return dict(id=identity, raw=raw, day=day, evaluated=evaluated, delivered=delivered,
                delivery=delivery, sources=sources, references=references,
                direction=assessment['direction'], fresh=assessment['fresh'], snapshot_known=snapshot_known)


def _bars(record, day, as_of, diagnostics):
    known, duplicates, seen = {}, set(), set()
    for bar in record['bars']:
        try:
            stamp, available = _time(bar['timestamp']), _time(bar['available_at'])
            if stamp in seen:
                duplicates.add(stamp)
                diagnostics['duplicate_bars'] += 1
            seen.add(stamp)
            if (stamp.second or stamp.microsecond or not _session(day, 9, 30) <= stamp < _session(day, *market_calendar.close_time(day))
                    or available < stamp + timedelta(minutes=1)):
                raise ValueError('Invalid bar timing')
            if available > as_of:
                diagnostics['future_bars'] += 1
                continue
            if not all(_number(bar[k]) and bar[k] > 0 for k in ('open', 'high', 'low', 'close')):
                raise ValueError('Invalid bar prices')
            if not bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high']:
                raise ValueError('Invalid OHLC geometry')
            known[stamp] = bar
        except (TypeError, ValueError, KeyError, OverflowError):
            diagnostics['invalid_bars'] += 1
    return {k: v for k, v in known.items() if k not in duplicates}


def _outcome(entry, horizon, known, as_of):
    outcome = entry['raw']['outcomes'][horizon]
    if outcome is None:
        return 'pending', None
    if not isinstance(outcome, dict) or outcome.get('status') not in OUTCOMES[:1] + OUTCOMES[2:5]:
        raise ValueError('Invalid outcome status')
    status = outcome['status']
    if status != 'complete':
        # Finalized missing stays missing: never run a grader to improve it.
        return (status if entry['snapshot_known'] else 'unknown'), None
    minutes, delivered, day = int(horizon), entry['delivered'], entry['day']
    expected_entry = delivered.replace(second=0, microsecond=0) + timedelta(minutes=1)
    expected_exit = expected_entry + timedelta(minutes=minutes)
    latest_end = _time(entry['raw']['assessment']['bar_at']) + timedelta(minutes=1)
    if (outcome['direction'] != entry['direction'] or outcome['horizon_minutes'] != minutes
            or not 0 <= (delivered - latest_end).total_seconds() <= 90
            or not entry['fresh'] or _time(outcome['entry_time']) != expected_entry
            or _time(outcome['exit_time']) != expected_exit
            or expected_entry < _session(day, 9, 30) or expected_exit > _session(day, *market_calendar.close_time(day))):
        raise ValueError('Noncausal outcome')
    if expected_exit > as_of:
        return 'pending', None
    if not all(_number(outcome.get(k)) for k in ('entry_price', 'exit_price', 'points')):
        raise ValueError('Nonfinite outcome')
    if outcome['entry_price'] <= 0 or outcome['exit_price'] <= 0:
        raise ValueError('Invalid prices')
    points = outcome['exit_price'] - outcome['entry_price']
    signed = points if entry['direction'] == 'UP' else -points if entry['direction'] == 'DOWN' else None
    if not math.isclose(points, outcome['points'], abs_tol=1e-7):
        raise ValueError('Wrong points')
    if signed is None:
        if outcome.get('signed_points') is not None:
            raise ValueError('Neutral must not be scored')
    elif not _number(outcome.get('signed_points')) or not math.isclose(signed, outcome['signed_points'], abs_tol=1e-7):
        raise ValueError('Wrong signed points')
    held = [known.get(expected_entry + timedelta(minutes=i)) for i in range(minutes)]
    if any(bar is None for bar in held):
        # Outcome may have been saved later than this retrospective cutoff.
        if not entry['snapshot_known']:
            return 'unknown', None
        raise ValueError('Missing outcome evidence')
    if not (math.isclose(held[0]['open'], outcome['entry_price'], abs_tol=1e-7)
            and math.isclose(held[-1]['close'], outcome['exit_price'], abs_tol=1e-7)):
        raise ValueError('Outcome disagrees with evidence')
    return 'complete', signed


def context_report(directory: Path, start: date, end: date, as_of: datetime, *, usage_pairs=None) -> list[str]:
    """Report [start,end) ET sessions at an aware instant, without modifying files.

    Optional usage_pairs accepts report_theta_audit._load_usage's validated pairs
    and avoids another telemetry scan. Without it, local usage JSONL is read.
    """
    if type(start) is not date or type(end) is not date or not 0 < (end - start).days <= 3660:
        raise ValueError('Invalid context report window')
    as_of, directory = _time(as_of), Path(directory)
    diagnostics = Counter()
    entries, conflicts, bars_by_day = {}, set(), {}
    sessions = [day for day in _days(start, min(end, as_of.astimezone(market_calendar.ET).date() + timedelta(days=1)))
                if market_calendar.is_trading_day(day) and _session(day, 9, 30) <= as_of]
    for day in sessions:
        path = directory / f'context-{day}.json'
        if not path.exists():
            diagnostics['missing_files'] += 1
            continue
        diagnostics['files'] += 1
        try:
            with path.open(encoding='utf-8') as handle:
                state = json.load(handle)
            record = state['record']
            if (type(state['schema']) is not int or state['schema'] != 1 or state['version'] != 'spx-context-v1'
                    or state['session_date'] != day.isoformat() or not isinstance(state['assessments'], list)
                    or type(record['schema']) is not int or record['schema'] != 1
                    or record['rule'] != 'spx-orb-15m-first-break-v1' or record['paper_only'] is not True
                    or record['session_date'] != day.isoformat() or not isinstance(record['bars'], list)):
                raise ValueError('Incompatible ledger')
            # last_poll_at is a request-start heartbeat, NOT a save/finalization
            # timestamp. File mtime is an extra conservative snapshot bound.
            snapshot = max(datetime.fromtimestamp(path.stat().st_mtime, UTC),
                           _time(state['last_poll_at']) if state.get('last_poll_at') else _session(day, 9, 30))
            snapshot_known = snapshot <= as_of
            diagnostics['future_snapshots'] += not snapshot_known
            bars_by_day[day] = _bars(record, day, as_of, diagnostics)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            diagnostics['corrupt_files'] += 1
            continue
        for raw in state['assessments']:
            try:
                entry = _entry(raw, day, as_of, snapshot_known)
                if entry is None:
                    diagnostics['after_cutoff'] += 1
                    continue
                identity = entry['id']
                if identity in entries:
                    if entries[identity]['raw'] == raw:
                        diagnostics['duplicates'] += 1
                    else:
                        diagnostics['conflicts'] += 1
                        conflicts.add(identity)
                    continue
                entries[identity] = entry
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                diagnostics['invalid_entries'] += 1
    views = [v for k, v in entries.items() if k not in conflicts]
    sent = [v for v in views if v['delivery'] == 'sent']
    deliveries = Counter(v['delivery'] for v in views)
    lines = ['', 'SPX CONTEXT NOTIFICATIONS / ACTUAL DATA INCORPORATION',
             f"Context files: expected={len(sessions)} present={diagnostics['files']} "
             f"valid={diagnostics['files']-diagnostics['corrupt_files']} missing={diagnostics['missing_files']} "
             f"corrupt={diagnostics['corrupt_files']}",
             f"Evaluated assessments={len(views)} " + ' '.join(f'{k}={deliveries[k]}' for k in DELIVERIES)]
    if not views:
        lines.append('NO CONTEXT EVIDENCE: absent files do not prove zero usage; the new stream may not be deployed or this may be the wrong directory.')
    lines.append('Only sent means acknowledged delivery; pending/unknown are not presumed delivered. Evaluated polls include suppressed descriptions, not just alerts.')
    for label, rows in (('ALL evaluated', views), ('SENT only', sent)):
        lines.append(f'Source incorporation / {label}:')
        for source in SOURCES:
            used = sum(v['sources'][source]['used'] for v in rows)
            statuses = Counter(v['sources'][source]['status'] for v in rows)
            lines.append(f"  {source}: used={used}/{len(rows)} unused={len(rows)-used}; "
                         + (' '.join(f'{k}={n}' for k, n in sorted(statuses.items())) or 'statuses=n/a'))
    completed = _telemetry(directory, start, end, as_of, usage_pairs, diagnostics)
    success = {k: v for k, v in completed.items() if v['success']}
    groups = defaultdict(set)
    for identity, row in completed.items():
        groups[(row['product'], row['purpose'])].add(identity)
    for label, rows in (('ALL evaluated', views), ('SENT only', sent)):
        consumed, product_by_ref = set(), defaultdict(set)
        causal = set()
        used_no_id = unused_refs = noncausal_refs = 0
        for entry in rows:
            for source, product in PAID.items():
                ids = entry['references'][source]
                if entry['sources'][source]['used']:
                    used_no_id += not ids
                    consumed.update(ids)
                    for identity in ids:
                        product_by_ref[identity].add(product)
                        if identity in completed:
                            if completed[identity]['finished_at'] <= entry['evaluated']:
                                causal.add(identity)
                            else:
                                noncausal_refs += 1
                else:
                    unused_refs += len(ids)
        matched = {k for k in consumed if k in success and k in causal
                   and product_by_ref[k] == {success[k]['product']}}
        failed_refs = {k for k in consumed if k in completed and not completed[k]['success']}
        mismatch = {k for k in consumed if k in success and product_by_ref[k] != {success[k]['product']}}
        unmatched = consumed - completed.keys()
        lines.append(f'Unique consumed Theta requests / {label}: referenced={len(consumed)} '
                     f'matched_success={len(matched)} unmatched={len(unmatched)} '
                     f'failed_completion_refs={len(failed_refs)} product_mismatch={len(mismatch)} '
                     f'noncausal_reference_occurrences={noncausal_refs} '
                     f'used_source_assessments_without_ids={used_no_id} ignored_refs_on_unused_sources={unused_refs}')
        for (product, purpose), ids in sorted(groups.items()):
            good = ids & success.keys()
            lines.append(f'  {product}/{purpose}: consumed_success={len(matched & good)} '
                         f'/ observed_success={len(good)}; observed_completions={len(ids)}')
        if not groups:
            lines.append('  No matching telemetry denominator available.')
    lines += ['Consumption unions retained IDs; cache reuse is counted once, not once per notification. '
              'Observed/acquisition IDs alone receive no consumption credit. Raw response rows are not unique useful data or billing units.',
              'Unmatched references may predate the audit, lack metadata, or be outside the cutoff; they are not automatically corrupt. '
              'Successful-completion denominators contain all observed product/purpose requests, including research overhead; '
              'the report does not measure all account/API usage.',
              'Flow lineage uses contributing cached-trade request IDs when present; older representative-only '
              'records are less precise. Repeated trades across requests are not unique observations. '
              'Yahoo SPY is a separate indicative proxy, not a Theta subscription request.',
              'Options-flow incorporation changes descriptive confirmation only; the price-derived direction is unchanged. '
              'Used does NOT mean predictive value. The separate frozen ORB paired-policy comparison remains the counterfactual experiment.',
              '', 'SENT CONTEXT / FORWARD SPX MOVEMENT (NOT STRATEGY RETURNS)']
    for horizon in ('15', '30'):
        counts, directional, neutral, signs = Counter(), [], 0, Counter()
        for entry in sent:
            try:
                status, signed = _outcome(entry, horizon, bars_by_day[entry['day']], as_of)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                status, signed = 'unknown', None
                diagnostics['invalid_outcomes'] += 1
            counts[status] += 1
            if status == 'complete':
                signs[entry['direction']] += 1
                if signed is None:
                    neutral += 1
                else:
                    directional.append(signed)
        average = f'{sum(v / len(directional) for v in directional):+.3f}' if directional else 'n/a'
        lines.append(f'{horizon}m: sent={len(sent)} ' + ' '.join(f'{k}={counts[k]}' for k in OUTCOMES)
                     + f"; complete_UP={signs['UP']} complete_DOWN={signs['DOWN']} neutral_complete={neutral} "
                     f'directional_n={len(directional)} mean_signed_SPX_points={average}')
    # Classify only explicitly recorded, valid actions. Never retrofit the new
    # checklist to old context rows or recompute finalized/pending outcomes.
    from zero_dte.spx_actions import VERSION, LABELS, CANDIDATES, validated_action
    action_rows = defaultdict(list)
    invalid_actions = unclassified = 0
    for entry in sent:
        assessment = entry['raw']['assessment']
        action = validated_action(assessment)
        if action is None:
            unclassified += 1
            invalid_actions += 'action' in assessment
        else:
            action_rows[action['state']].append(entry)
    lines += ['', f'EXPERIMENTAL ACTIONS / SENT ONLY / {VERSION}',
              f'Recorded valid actions={sum(map(len, action_rows.values()))} unclassified={unclassified} '
              f'invalid_or_unknown_version={invalid_actions}',
              'Price-only checklist: SPX is the rule input/quality gate. Prior levels, SPY and options flow '
              'are contextual only, never action gates; their displayed incorporation earns no action-input credit. '
              'SPX request lineage is retained in each assessment consumed_request_ids.spx.']
    for code, label in LABELS.items():
        rows = action_rows[code]
        lines.append(f'{label}: sent={len(rows)}')
        if code not in CANDIDATES:
            lines.append('  Non-entry action: no directional action score, no invented zero return or avoided-loss credit.')
            continue
        for horizon in ('15', '30'):
            counts, points = Counter(), []
            for entry in rows:
                try:
                    status, signed = _outcome(entry, horizon, bars_by_day[entry['day']], as_of)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    status, signed = 'unknown', None
                counts[status] += 1
                if status == 'complete' and signed is not None:
                    points.append(signed)
            mean = f'{sum(points) / len(points):+.3f}' if points else 'n/a'
            lines.append(f'  {horizon}m: ' + ' '.join(f'{k}={counts[k]}' for k in OUTCOMES)
                         + f'; directional_n={len(points)} mean_signed_SPX_points={mean}')
    lines.append('Action outcomes reuse validated delivery-time SPX horizons; no option P&L, fills, '
                 'position management, independent-trade count, or demonstrated edge. Frozen ORB comparison unchanged.')
    lines += ['Neutral records retain forward movement but have no signed score or invented correctness. '
              'Repeated/overlapping notifications are correlated, not independent trades. No accuracy, confidence, option P&L, costs or profitability claim is computed.',
              'Finalized missing/invalid/session-ended outcomes are never repaired by backfill. Schema-1 mutable terminal states '
              'without an as-of-known snapshot are unknown, not losses or zero returns. Complete outcomes require causal delivery timing, '
              'prices and contiguous bars received by the cutoff; pending outcomes are not upgraded by this report.',
              'Snapshot availability uses file mtime AND last_poll_at because the heartbeat is a request-start time, not a finalization timestamp. '
              'Copying files can conservatively withhold historical terminal states. Delivery statuses without event timestamps are similarly withheld.',
              'Context integrity: ' + ' '.join(f'{k}={diagnostics[k]}' for k in (
                  'invalid_entries', 'duplicates', 'conflicts', 'after_cutoff', 'future_snapshots', 'invalid_outcomes',
                  'invalid_bars', 'duplicate_bars', 'future_bars',
                  'usage_invalid', 'usage_duplicates', 'usage_conflicts', 'usage_unreadable')),
              'Conflicting assessment IDs are excluded entirely. This section is read-only and makes no automatic subscription decision.']
    return lines
