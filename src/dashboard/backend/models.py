from datetime import datetime, timezone
from .security import sanitize


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def envelope(data, availability='AVAILABLE', sources=(), warnings=()):
    return sanitize(dict(schema='DASHBOARD_API_V1', data=data, availability=availability,
        observed_at=now().isoformat(), sources=list(sources), warnings=list(warnings)))


def payload(row):
    value = row.get('payload', {})
    if not isinstance(value, dict):
        return {}
    nested = value.get('value')
    return nested if isinstance(nested, dict) and 'schema' in value else value
