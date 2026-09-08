"""Live SPX service settings only. Research has its own configuration.

Collection and publication are explicit opt-ins. Obsolete feature variables are
never parsed. Old SPX channel/poll names are migration aliases, not legacy modes.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in ('1', 'true'):
        return True
    if value in ('0', 'false', ''):
        return False
    raise ValueError(f'{name} must be true/false or 1/0')


def _integer(name, default, *, minimum=0, maximum=None, alias=None):
    value = os.getenv(name)
    if value is None and alias:
        value = os.getenv(alias)
    try:
        number = int(default if value is None else value)
    except (ValueError, TypeError):
        raise ValueError(f'{name} must be an integer') from None
    if number < minimum or (maximum is not None and number > maximum):
        raise ValueError(f'{name} is outside its allowed range')
    return number


DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
THETADATA_API_KEY = os.getenv('THETADATA_API_KEY', '').strip()
COMMAND_PREFIX = '!'
COLLECTION_ENABLED = _flag('COLLECTION_ENABLED')
PUBLICATION_ENABLED = _flag('PUBLICATION_ENABLED')
SPX_CHANNEL_ID = _integer('SPX_CHANNEL_ID', 0, alias='SPX_CONTEXT_CHANNEL_ID')
SPX_POLL_SECONDS = _integer('SPX_POLL_SECONDS', 60, minimum=30, maximum=60,
                            alias='SPX_CONTEXT_POLL_SECONDS')

# Optional request-metadata window, not a strategy or a collection switch.
# Preserve the original ET start date. Expiry never stops vendor billing.
THETA_AUDIT_START_DATE = os.getenv('THETA_AUDIT_START_DATE', '').strip()
try:
    THETA_AUDIT_DAYS = int(os.getenv('THETA_AUDIT_DAYS', '30'))
except ValueError:
    THETA_AUDIT_DAYS = 0  # Invalid metadata settings disable recording only.
