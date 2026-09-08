"""Read-only operational review of an existing market-state session.

This reader never starts collection, migrates evidence or resolves pending
attempts. CSV export is an explicit, exclusive-create owner review worksheet,
not a predictive evaluation or a market-data export.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, timedelta
import json
from pathlib import Path
import sys

from core.market_calendar import ET
from core.paths import THETADATA_DATA_DIR
from core.session_bars import utc
from core.state_store import DELIVERY_STATES, DIRECTIONS, VERSION, validate_state


class ReviewError(ValueError):
    """Safe user-facing error without source data or exception payloads."""


def _reject_constant(_value):
    raise ValueError('Nonfinite JSON number')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def load_session(directory: Path, day: date) -> dict:
    """Read exactly one existing ledger. No StateStore.load or migrations."""
    try:
        with (Path(directory) / f'state-{day.isoformat()}.json').open(encoding='utf-8') as handle:
            state = json.load(handle, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        # validate_state also rejects overflowed JSON numbers such as 1e999.
        validate_state(state, day)
    except OSError:
        raise ReviewError('Cannot read the requested existing session file.') from None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise ReviewError('Session evidence is invalid or incompatible; no export created.') from None
    return state


def summary(state: dict) -> str:
    """Summarize already validated evidence without modifying pending entries."""
    observations = state['observations']
    directions = Counter(item['snapshot']['classification']['direction'] for item in observations)
    volatile = sum(item['snapshot']['classification']['volatile'] for item in observations)
    deliveries = Counter(entry['delivery'] for entry in state['deliveries'])
    transitions = sum(item['transition'] for item in observations)
    lines = [f"SPX operational review — {state['session_date']} — {VERSION}",
             f"Local completed minute bars: {len(state['record']['bars'])}; "
             f"prior-history bars: {len(state['previous_bars'])}",
             f"Migration sources recorded: {len(state['migration'])}"]
    for kind in ('current', 'prior'):
        counts = state['requests'][kind]
        incomplete = counts['started'] - counts['succeeded'] - counts['failed']
        lines.append(f"{kind.capitalize()} requests: attempted={counts['started']}, "
                     f"succeeded={counts['succeeded']}, failed={counts['failed']}, incomplete={incomplete}")
    lines.extend([
        f"Recorded observations: {len(observations)}; transitions: {transitions}; "
        f"non-transition observations: {len(observations) - transitions}",
        'Direction: ' + ', '.join(f'{key}={directions[key]}' for key in DIRECTIONS),
        f'Volatility (independent of direction): volatile={volatile}, '
        f'not_volatile={len(observations) - volatile}',
        'Transition deliveries: ' + ', '.join(f'{key}={deliveries[key]}' for key in DELIVERY_STATES),
        'Incomplete requests and pending deliveries remain unresolved as recorded; this reader does not reconcile them.',
        'Observation counts are the recorded fresh classifications, not full-session coverage or all market events.',
        'Missing/unavailable classifications, missed opportunities and owner usefulness are not measured by these counts.',
        'Request attempts are not billing units. Sent includes any stale acknowledgements recorded by the service.',
        'BB/market-state outcomes, win rate, predictive edge and option P&L: not evaluated.',
    ])
    return '\n'.join(lines)


CSV_FIELDS = ('observation_id', 'candle_completed_et', 'direction', 'volatile',
              'transition', 'delivery_status', 'usefulness', 'notes')


def review_rows(state: dict) -> list[dict]:
    deliveries = {entry['observation_id']: entry['delivery'] for entry in state['deliveries']}
    rows = []
    for item in state['observations']:
        classification = item['snapshot']['classification']
        ended = (utc(classification['bar_at']) + timedelta(minutes=5)).astimezone(ET)
        status = deliveries.get(item['id'])
        if status is None:
            status = 'initial_silent' if item['previous_state'] is None else 'unchanged'
        rows.append(dict(observation_id=item['id'], candle_completed_et=ended.isoformat(),
                         direction=classification['direction'], volatile=str(classification['volatile']).lower(),
                         transition=str(item['transition']).lower(), delivery_status=status,
                         usefulness='', notes=''))
    return rows


def export_csv(path: Path, state: dict) -> None:
    rows = review_rows(state)  # Build everything before creating the requested file.
    try:
        with Path(path).open('x', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    except FileExistsError:
        raise ReviewError('CSV output already exists; nothing was overwritten.') from None
    except (OSError, ValueError, csv.Error):
        raise ReviewError('Cannot create or finish the requested CSV export; check for a partial output.') from None


def _session_date(value):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError
        return day
    except ValueError:
        raise argparse.ArgumentTypeError('Session date must use YYYY-MM-DD.') from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Read-only SPX session operations and owner-review worksheet; no collection or performance grading.')
    parser.add_argument('session_date', type=_session_date, help='Existing session in YYYY-MM-DD form')
    parser.add_argument('--directory', type=Path, default=Path(THETADATA_DATA_DIR) / 'market-state',
                        help='Existing market-state evidence directory')
    parser.add_argument('--csv', type=Path, metavar='PATH', help='Create a new owner-review CSV; never overwrite')
    args = parser.parse_args(argv)
    try:
        state = load_session(args.directory, args.session_date)
        report = summary(state)
        if args.csv is not None:
            export_csv(args.csv, state)
    except ReviewError as exc:
        print(f'Review error: {exc}', file=sys.stderr)
        return 1
    print(report)
    if args.csv is not None:
        print('Owner-review CSV created; usefulness and notes are blank for manual review.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
