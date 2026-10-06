"""Output redaction and same-origin restrictions; no credentials in DTOs."""
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
import math
import json
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

SECRET_KEY = re.compile(r'password|passwd|passphrase|apikey|apisecret|accesstoken|refreshtoken|authorization|privatekey|secret|dsn|connectionstring|credential|^token$')
ASSIGN = re.compile(r'''(?ix)(?<![a-z0-9])"?(?:password|passwd|passphrase|api[_ -]?key|api[_ -]?secret|access[_ -]?token|refresh[_ -]?token|token|secret|dsn|private[_ -]?key|connection[_ -]?string|credential)"?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;&}]+)''')
URL = re.compile(r'''\b[a-z][a-z0-9+.-]*://[^\s<>"']+''', re.I)
PEM = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S)
JWT = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')
AUTH = re.compile(r'(?i)(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+[^\s,;]+')


def _url(match):
    try:
        parts = urlsplit(match.group(0))
        if parts.scheme.lower() in {'postgres', 'postgresql'}:
            return '[REDACTED_DSN]'
        host = parts.netloc.rsplit('@', 1)[-1]
        return urlunsplit((parts.scheme, host, parts.path, '', ''))
    except ValueError:
        return '[REDACTED_URL]'


def sanitize(value, depth=0):
    if depth > 24:
        return '[DEPTH_LIMIT]'
    if isinstance(value, Mapping):
        return {str(key): ('[REDACTED]' if SECRET_KEY.search(re.sub(r'[^a-z0-9]', '', str(key).lower()))
            else sanitize(item, depth+1)) for key, item in list(value.items())[:256]}
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth+1) for item in value[:2000]]
    if isinstance(value, str):
        value = value[:16384]
        if value.lstrip().startswith(('{', '[')):
            try:
                structured = json.loads(value)
            except (ValueError, TypeError, RecursionError):
                structured = None
            if isinstance(structured, (Mapping, list)):
                cleaned = sanitize(structured, depth+1)
                return json.dumps(cleaned, ensure_ascii=False, separators=(',', ':'))[:16384]
        value = PEM.sub('[REDACTED_PRIVATE_KEY]', value)
        value = JWT.sub('[REDACTED_TOKEN]', value)
        value = AUTH.sub('[REDACTED_AUTH]', value)
        value = URL.sub(_url, value)
        value = ASSIGN.sub('[REDACTED_CREDENTIAL]', value)
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return '[UNSUPPORTED_VALUE]'


def trusted_request(request, port):
    allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
    if request.headers.get('host', '').lower() not in allowed:
        return False
    origin = request.headers.get('origin')
    if origin and origin not in {f'http://{host}' for host in allowed}:
        return False
    return request.headers.get('sec-fetch-site') not in {'cross-site', 'same-site'}
