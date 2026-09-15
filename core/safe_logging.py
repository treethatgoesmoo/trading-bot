"""Diagnostic locations without exception messages, source lines or locals."""
from pathlib import Path
import traceback


def failure_location(error):
    parts, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        frames = traceback.extract_tb(error.__traceback__)
        locations = ','.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in frames)
        parts.append(f'{type(error).__name__}[{locations}]')
        error = error.__cause__ or error.__context__
    return ' <- '.join(parts)
