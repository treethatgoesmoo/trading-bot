"""Validated atomic market-state evidence and read-only legacy migration.

A new namespace never reuses old BB deliveries as state alerts. Legacy files
remain byte-for-byte untouched; hashes identify the acquisition evidence used.
This module does not import or execute historical strategies or their graders.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from core import market_calendar
from core.session_bars import new_record, session_time, utc, validate_record

VERSION = 'spx-state-v1'
SCHEMA = 1
DIRECTIONS = ('bullish', 'bearish', 'neutral')
DELIVERY_STATES = ('pending', 'sent', 'failed', 'unknown', 'suppressed')


def preceding_sessions(day):
    days = []
    while len(days) < 2:
        day -= timedelta(days=1)
        if market_calendar.is_trading_day(day):
            days.append(day)
    return days


def state_key(classification):
    direction, volatile = classification['direction'], classification['volatile']
    if direction not in DIRECTIONS or type(volatile) is not bool:
        raise ValueError('Invalid observed market state')
    return dict(direction=direction, volatile=volatile)


def observation_id(day, classification):
    key = state_key(classification)
    stamp = utc(classification['bar_at']).isoformat()
    return f"{VERSION}/{day}/{stamp}/{key['direction']}/{int(key['volatile'])}"


def new_state(day):
    return dict(schema=SCHEMA, version=VERSION, session_date=day.isoformat(),
                record=new_record(day), previous_bars=[], prior_complete=False,
                prior_sessions={d.isoformat(): dict(complete=False, retry_at=None)
                                for d in preceding_sessions(day)},
                bar_request_ids={}, prior_bar_request_ids={}, snapshot=None,
                observed_bar=None, last_observed_state=None, last_acknowledged_state=None,
                observations=[], deliveries=[], polls=0, last_poll_at=None,
                poll_status='new', migration=[],
                requests={kind: dict(started=0, succeeded=0, failed=0) for kind in ('current', 'prior')})


def _counts(counts):
    if (not isinstance(counts, dict) or any(type(counts.get(k)) is not int or counts[k] < 0
                                          for k in ('started', 'succeeded', 'failed'))
            or counts['succeeded'] + counts['failed'] > counts['started']):
        raise ValueError('Invalid request counters')


def _lineage(values, bars):
    stamps = {b['timestamp'] for b in bars}
    if (not isinstance(values, dict) or any(stamp not in stamps or
            (rid is not None and (not isinstance(rid, str) or not rid)) for stamp, rid in values.items())):
        raise ValueError('Invalid bar request lineage')


def _prior(state, day):
    expected_days = preceding_sessions(day)
    metadata = state['prior_sessions']
    if (not isinstance(state['previous_bars'], list) or not isinstance(metadata, dict)
            or set(metadata) != {d.isoformat() for d in expected_days}):
        raise ValueError('Invalid prior-session metadata')
    grouped = {d: [] for d in expected_days}
    last = None
    for bar in state['previous_bars']:
        stamp = utc(bar['timestamp'])
        prior_day = stamp.astimezone(market_calendar.ET).date()
        if prior_day not in grouped or (last is not None and stamp <= last):
            raise ValueError('Unexpected or unordered prior session')
        grouped[prior_day].append(bar)
        last = stamp
    for prior_day, rows in grouped.items():
        validate_record(dict(new_record(prior_day), bars=rows), prior_day)
        meta = metadata[prior_day.isoformat()]
        count = int((session_time(prior_day, *market_calendar.close_time(prior_day))
                     - session_time(prior_day, 9, 30)).total_seconds() / 60)
        if (not isinstance(meta, dict) or type(meta.get('complete')) is not bool
                or meta['complete'] != (len(rows) == count) or 'retry_at' not in meta):
            raise ValueError('Contradictory prior-session completeness')
        if meta['retry_at'] is not None:
            utc(meta['retry_at'])
    if (type(state['prior_complete']) is not bool or state['prior_complete'] !=
            all(meta['complete'] for meta in metadata.values())):
        raise ValueError('Invalid aggregate prior completeness')
    _lineage(state['prior_bar_request_ids'], state['previous_bars'])


def _snapshot(snapshot, day, *, observation=False):
    if (not isinstance(snapshot, dict) or snapshot.get('version') != VERSION
            or snapshot.get('session_date') != day.isoformat()
            or type(snapshot.get('fresh')) is not bool or not isinstance(snapshot.get('timeframes'), dict)
            or set(snapshot['timeframes']) != {'1', '5', '15'}):
        raise ValueError('Invalid state snapshot')
    when = utc(snapshot['as_of'])
    classification = snapshot['classification']
    if not isinstance(classification, dict) or type(classification.get('available')) is not bool:
        raise ValueError('Invalid classification availability')
    if snapshot.get('bar_at') is not None:
        minute = utc(snapshot['bar_at'])
        if (minute.second or minute.microsecond or not
                session_time(day, 9, 30) <= minute < session_time(day, *market_calendar.close_time(day))
                or minute + timedelta(minutes=1) > when):
            raise ValueError('Invalid snapshot minute')
    if classification['available']:
        state_key(classification)
        stamp = utc(classification['bar_at'])
        offset = (stamp - session_time(day, 9, 30)).total_seconds()
        if (not snapshot['fresh'] or snapshot.get('bar_at') is None or offset < 0 or offset % 300
                or stamp + timedelta(minutes=5) > utc(snapshot['bar_at']) + timedelta(minutes=1)
                or not isinstance(classification.get('reasons'), list)
                or not all(isinstance(v, str) for v in classification['reasons'])):
            raise ValueError('Invalid classification candle')
    elif classification.get('direction') != 'unavailable' or classification.get('volatile') is not None:
        raise ValueError('Unavailable classification claims a state')
    if observation:
        if (not classification['available'] or not snapshot['fresh'] or
                not 0 <= (when - utc(classification['bar_at']) - timedelta(minutes=5)).total_seconds() <= 90
                or not 0 <= (when - utc(snapshot['bar_at']) - timedelta(minutes=1)).total_seconds() <= 90):
            raise ValueError('Observation was not fresh at assessment')
    return classification


def validate_state(state, day):
    """Reject corruption before it can reset a transition or resend an event."""
    if (not isinstance(state, dict) or type(state.get('schema')) is not int or state['schema'] != SCHEMA
            or state.get('version') != VERSION or state.get('session_date') != day.isoformat()
            or not market_calendar.is_trading_day(day)):
        raise ValueError('Incompatible market-state evidence')
    # Reject non-JSON numbers even in fields not interpreted by this reader.
    json.dumps(state, allow_nan=False)
    validate_record(state['record'], day)
    _lineage(state['bar_request_ids'], state['record']['bars'])
    _prior(state, day)
    for kind in ('current', 'prior'):
        _counts(state['requests'][kind])
    if type(state['polls']) is not int or state['polls'] < 0 or state['polls'] != state['record']['polls']:
        raise ValueError('Invalid poll count')
    if state['last_poll_at'] is not None:
        utc(state['last_poll_at'])
    if state['snapshot'] is not None:
        _snapshot(state['snapshot'], day)
    if not isinstance(state['migration'], list):
        raise ValueError('Invalid migration provenance')
    for source in state['migration']:
        path = Path(source['path'])
        if (path.is_absolute() or '..' in path.parts or not re.fullmatch('[0-9a-f]{64}', source['sha256'])
                or source['kind'] not in ('bb-events-v1', 'spx-context-v1', 'spx-orb-15m-first-break-v1')):
            raise ValueError('Invalid migration source')
    if not isinstance(state['observations'], list) or not isinstance(state['deliveries'], list):
        raise ValueError('Invalid evidence ledger')
    previous, last_bar, seen = None, None, {}
    stamps = {b['timestamp'] for b in state['record']['bars']}
    for item in state['observations']:
        classification = _snapshot(item['snapshot'], day, observation=True)
        stamp, key = utc(classification['bar_at']), state_key(classification)
        expected_id = observation_id(day.isoformat(), classification)
        transition = key != previous if previous is not None else (key['direction'] != 'neutral' or key['volatile'])
        prefix = [b for b in state['record']['bars'] if utc(b['timestamp']) <= utc(item['snapshot']['bar_at'])]
        opening = session_time(day, 9, 30)
        if (len(prefix) != int((utc(item['snapshot']['bar_at']) - opening).total_seconds() / 60) + 1
                or any(utc(b['timestamp']) != opening + timedelta(minutes=i)
                       or utc(b['available_at']) > utc(item['evaluated_at']) for i, b in enumerate(prefix))):
            raise ValueError('Observation lacks causal complete minute evidence')
        for prior_day in item['snapshot'].get('prior_days', []):
            if (prior_day not in state['prior_sessions'] or not state['prior_sessions'][prior_day]['complete']
                    or any(utc(b['available_at']) > utc(item['evaluated_at']) for b in state['previous_bars']
                           if utc(b['timestamp']).astimezone(market_calendar.ET).date().isoformat() == prior_day)):
                raise ValueError('Observation claims unavailable history')
        if (item['id'] != expected_id or item['id'] in seen or (last_bar is not None and stamp <= last_bar)
                or item['previous_state'] != previous or type(item['transition']) is not bool
                or item['transition'] != transition or item['outcome_status'] != 'not_evaluated'
                or utc(item['evaluated_at']) != utc(item['snapshot']['as_of'])
                or any((stamp + timedelta(minutes=i)).isoformat() not in stamps for i in range(5))):
            raise ValueError('Inconsistent observation ledger')
        consumed = item['consumed_request_ids']
        if (not isinstance(consumed, dict) or set(consumed) != {'spx', 'prior_spx'}
                or any(not isinstance(ids, list) or any(not isinstance(v, str) or not v for v in ids)
                       or ids != sorted(set(ids)) for ids in consumed.values())):
            raise ValueError('Invalid observation request lineage')
        for kind, source in (('spx', state['bar_request_ids']), ('prior_spx', state['prior_bar_request_ids'])):
            if not set(consumed[kind]).issubset({v for v in source.values() if isinstance(v, str)}):
                raise ValueError('Unknown consumed request')
        seen[item['id']] = item
        previous, last_bar = key, stamp
    if (state['last_observed_state'] != previous
            or state['observed_bar'] != (last_bar.isoformat() if last_bar is not None else None)):
        raise ValueError('Inconsistent last observed state')
    delivery_ids, acknowledged = set(), None
    previous_index = -1
    ordered = {item['id']: i for i, item in enumerate(state['observations'])}
    for entry in state['deliveries']:
        item = seen.get(entry['id'])
        if (item is None or not item['transition'] or entry['id'] in delivery_ids
                or entry['observation_id'] != entry['id'] or entry['delivery'] not in DELIVERY_STATES
                or type(entry['attempts']) is not int or entry['attempts'] < 0
                or ordered[entry['id']] <= previous_index):
            raise ValueError('Invalid delivery ledger')
        delivery_ids.add(entry['id'])
        previous_index = ordered[entry['id']]
        if entry['attempted_at'] is not None:
            if utc(entry['attempted_at']) < utc(item['evaluated_at']) or not entry['attempts']:
                raise ValueError('Invalid delivery attempt')
        elif entry['attempts']:
            raise ValueError('Missing delivery attempt time')
        if entry['delivery'] in ('sent', 'unknown') and not entry['attempts']:
            raise ValueError('Unattempted delivery cannot be acknowledged or ambiguous')
        if entry['delivered_at'] is not None:
            if (entry['delivery'] != 'sent' or entry['attempted_at'] is None
                    or utc(entry['delivered_at']) < utc(entry['attempted_at'])):
                raise ValueError('Invalid delivery acknowledgement')
            acknowledged = state_key(item['snapshot']['classification'])
        elif entry['delivery'] == 'sent':
            raise ValueError('Missing acknowledgement timestamp')
    if delivery_ids != {item['id'] for item in state['observations'] if item['transition']}:
        raise ValueError('Missing transition delivery intent')
    if state['last_acknowledged_state'] != acknowledged:
        raise ValueError('Inconsistent last acknowledged state')


def _legacy_record(record, day):
    if (not isinstance(record, dict) or type(record.get('schema')) is not int or record['schema'] != 1
            or record.get('rule') != 'spx-orb-15m-first-break-v1' or record.get('paper_only') is not True
            or record.get('session_date') != day.isoformat()
            or not isinstance(record.get('bars'), list)
            or any(key not in record for key in ('polls', 'fetch_failures', 'rejected_bars', 'candidate', 'outcome'))):
        raise ValueError('Invalid legacy record')
    result = new_record(day)
    for key in ('bars', 'polls', 'fetch_failures', 'rejected_bars', 'last_poll_at', 'last_success_at'):
        if key in record:
            result[key] = copy.deepcopy(record[key])
    validate_record(result, day)
    candidate = record.get('candidate')
    if candidate is not None:
        if (not isinstance(candidate, dict) or candidate.get('status') not in ('prospective', 'missed')
                or candidate.get('direction') not in ('UP', 'DOWN')):
            raise ValueError('Invalid legacy candidate')
        utc(candidate['available_at'])
        utc(candidate['signal_bar'])
        if candidate['status'] == 'prospective':
            utc(candidate['entry_time'])
    if record.get('outcome') is not None and not isinstance(record['outcome'], dict):
        raise ValueError('Invalid legacy outcome')
    return result


def _legacy_deliveries(legacy, day):
    if not isinstance(legacy.get('deliveries'), list):
        raise ValueError('Invalid legacy BB deliveries')
    seen = set()
    for entry in legacy['deliveries']:
        snapshot, event_ids = entry['snapshot'], entry['event_ids']
        if (entry['delivery'] not in DELIVERY_STATES or snapshot['version'] != 'bb-events-v1'
                or snapshot['session_date'] != day.isoformat() or not isinstance(event_ids, list)
                or not event_ids or event_ids != [event['id'] for event in snapshot['events']]
                or len(set(event_ids)) != len(event_ids) or seen.intersection(event_ids)):
            raise ValueError('Invalid legacy BB delivery ledger')
        for event in snapshot['events']:
            if (event['timeframe'] not in ('1', '5', '15') or event['kind'] not in
                    {f'{side}_{kind}' for side in ('upper', 'lower') for kind in
                     ('touch', 'close_outside', 'rejection', 'continuation')}
                    or event['id'] != f"bb-events-v1/{event['timeframe']}/{event['bar_at']}/{event['kind']}"):
                raise ValueError('Invalid legacy BB event')
            stamp = utc(event['bar_at'])
            offset = (stamp - session_time(day, 9, 30)).total_seconds()
            if offset < 0 or offset % (60 * int(event['timeframe'])) or stamp >= session_time(day, *market_calendar.close_time(day)):
                raise ValueError('Invalid legacy BB event candle')
        seen.update(event_ids)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _read(path):
    raw = path.read_bytes()
    def reject_constant(_value):
        raise ValueError('Nonfinite JSON number')
    def finite_float(value):
        import math
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('Nonfinite JSON number')
        return parsed
    return json.loads(raw, object_pairs_hook=_object, parse_constant=reject_constant,
                      parse_float=finite_float), raw


class StateStore:
    def __init__(self, directory, *, audit_directory=None):
        self.directory = Path(directory)
        self.root = self.directory.parent
        self.audit_directory = Path(audit_directory) if audit_directory is not None else self.root / 'audit'

    def path(self, day):
        return self.directory / f'state-{day}.json'

    def _source(self, path, kind):
        value, raw = _read(path)
        return value, dict(path=f'{path.parent.name}/{path.name}', kind=kind,
                           sha256=hashlib.sha256(raw).hexdigest())

    def _migrate(self, day):
        state = new_state(day)
        bb = self.root / 'bb-events' / f'bb-events-{day}.json'
        if bb.exists():
            legacy, source = self._source(bb, 'bb-events-v1')
            if (type(legacy.get('schema')) is not int or legacy['schema'] != 1
                    or legacy.get('version') != 'bb-events-v1' or legacy.get('session_date') != day.isoformat()):
                raise ValueError('Invalid legacy BB envelope')
            state['record'] = _legacy_record(legacy['record'], day)
            for key in ('previous_bars', 'prior_complete', 'prior_sessions', 'bar_request_ids', 'prior_bar_request_ids'):
                state[key] = copy.deepcopy(legacy[key])
            _prior(state, day)
            _lineage(state['bar_request_ids'], state['record']['bars'])
            for kind in ('current', 'prior'):
                _counts(legacy['requests'][kind])
            _legacy_deliveries(legacy, day)
            if (type(legacy.get('polls')) is not int or legacy['polls'] < 0
                    or legacy['polls'] != legacy['record']['polls']):
                raise ValueError('Invalid legacy poll count')
            if legacy['observed_bar'] is not None:
                if legacy['observed_bar'] not in {b['timestamp'] for b in state['record']['bars']}:
                    raise ValueError('Invalid legacy observed minute')
            if legacy.get('snapshot') is not None:
                if (not isinstance(legacy['snapshot'], dict)
                        or legacy['snapshot'].get('version') != 'bb-events-v1'
                        or legacy['snapshot'].get('session_date') != day.isoformat()):
                    raise ValueError('Invalid legacy cached snapshot')
            state['migration'] = [source]
        else:
            context = self.root / 'context' / f'context-{day}.json'
            mirror = self.audit_directory / f'orb-{day}.json'
            old_record = None
            if context.exists():
                legacy, source = self._source(context, 'spx-context-v1')
                if (type(legacy.get('schema')) is not int or legacy['schema'] != 1
                        or legacy.get('version') != 'spx-context-v1' or legacy.get('session_date') != day.isoformat()):
                    raise ValueError('Invalid legacy context envelope')
                old_record = legacy['record']
                state['record'] = _legacy_record(old_record, day)
                state['bar_request_ids'] = copy.deepcopy(legacy['bar_request_ids'])
                _lineage(state['bar_request_ids'], state['record']['bars'])
                state['migration'].append(source)
            if mirror.exists():
                legacy, source = self._source(mirror, 'spx-orb-15m-first-break-v1')
                record = _legacy_record(legacy, day)
                if old_record is not None and old_record != legacy:
                    raise ValueError('Conflicting context and ORB migration sources')
                state['record'] = record
                state['migration'].append(source)
        # New service counters start at zero. The original counters and all
        # experiment/delivery evidence remain intact in the hashed source file.
        state['record']['polls'] = 0
        state['record']['fetch_failures'] = 0
        state['record'].pop('last_poll_at', None)
        validate_state(state, day)
        return state

    def load(self, day):
        path = self.path(day)
        state = _read(path)[0] if path.exists() else self._migrate(day)
        validate_state(state, day)
        for entry in state['deliveries']:
            if entry['delivery'] == 'pending':
                # An unattempted intent is definitely unsent but is not replayed.
                entry.update(delivery='unknown' if entry['attempts'] else 'suppressed',
                             reason='interrupted_delivery' if entry['attempts'] else 'interrupted_before_attempt')
        validate_state(state, day)
        return state

    def save(self, state):
        day = date.fromisoformat(state['session_date'])
        validate_state(state, day)
        path = self.path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(state, handle, sort_keys=True, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            # POSIX directory fsync makes the rename durable across a crash.
            if os.name == 'posix':
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
