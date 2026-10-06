import hashlib
import re
from .models import envelope, payload, timestamp
from .security import sanitize

LINE = re.compile(r'^(\S+)\s+(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+([\w.:-]+)\s+(.*)$')
PEM_BEGIN = re.compile(r'-----BEGIN ([^-]*PRIVATE KEY)-----')
PEM_END = re.compile(r'-----END ([^-]*PRIVATE KEY)-----')


def _redact_private_blocks(lines, truncated_prefix=False):
    safe, open_type = [], None
    unknown_prefix = truncated_prefix
    for line in lines:
        if unknown_prefix or open_type:
            end = PEM_END.search(line)
            if end and (open_type is None or end.group(1) == open_type):
                unknown_prefix, open_type = False, None
                line = line[end.end():]
                if not line:
                    continue
            elif unknown_prefix and LINE.match(line):
                unknown_prefix = False
            else:
                continue
        while line:
            opening, end = PEM_BEGIN.search(line), PEM_END.search(line)
            if end and (opening is None or end.start() < opening.start()):
                # The BEGIN line may have been dropped at the byte-tail boundary.
                # Earlier lines could be key material, so discard this file prefix.
                safe = ['[REDACTED_TRUNCATED_PRIVATE_KEY]']
                line = line[end.end():]
                if not line:
                    break
                continue
            if opening is None:
                safe.append(line)
                break
            key_type = opening.group(1)
            matching_end = PEM_END.search(line, opening.end())
            while matching_end and matching_end.group(1) != key_type:
                matching_end = PEM_END.search(line, matching_end.end())
            prefix = line[:opening.start()]
            replacement = prefix + '[REDACTED_PRIVATE_KEY]'
            if matching_end:
                safe.append(replacement + line[matching_end.end():])
                break
            if not prefix and safe and LINE.match(safe[-1]):
                safe[-1] = safe[-1] + ' [REDACTED_PRIVATE_KEY]'
            else:
                safe.append(replacement)
            open_type = key_type
            break
    return safe


class EventReader:
    def __init__(self, catalog, db):
        self.catalog = catalog
        self.db = db

    def list(self, level=None, module=None, q=None, limit=100):
        rows, warnings = [], []
        for ref in self.catalog.scan():
            if not ref.endswith('.log'):
                continue
            try:
                lines, truncated = self.catalog.tail(ref, with_truncation=True)
                lines = _redact_private_blocks(lines, truncated_prefix=truncated)[-200:]
                index = 0
                while index < len(lines):
                    event_index = index
                    line = lines[index]
                    match = LINE.match(line)
                    at, severity, component, message = match.groups() if match else (None,'UNKNOWN','UNKNOWN',line)
                    opening = re.search(r'-----BEGIN ([^-]*PRIVATE KEY)-----', message)
                    prefix = ''
                    start = index
                    if (not opening and index + 1 < len(lines)
                            and LINE.match(lines[index + 1]) is None):
                        opening = re.search(r'-----BEGIN ([^-]*PRIVATE KEY)-----', lines[index + 1])
                        if opening:
                            start = index + 1
                            prefix = message + '\n' + lines[start][:opening.start()]
                    if opening:
                        closing = f"-----END {opening.group(1)}-----"
                        if start == index:
                            prefix, _, remainder = message.partition(opening.group(0))
                        else:
                            remainder = lines[start][opening.end():]
                        chunks = [remainder]
                        end = start
                        while closing not in '\n'.join(chunks) and end + 1 < len(lines):
                            end += 1
                            chunks.append(lines[end])
                        if closing in '\n'.join(chunks):
                            message = prefix + opening.group(0) + '\n'.join(chunks)
                        else:
                            # A truncated log must not expose an unterminated PEM body.
                            message = prefix + '[REDACTED_INCOMPLETE_PRIVATE_KEY]'
                        index = end
                    rows.append(dict(event_id=hashlib.sha256(f'{ref}:{event_index}:{line}'.encode()).hexdigest()[:24],
                        time=at if timestamp(at) else None, level='WARNING' if severity == 'WARN' else severity,
                        module=component, message=message, source=ref, correlation_id=None))
                    index += 1
            except (OSError,ValueError):
                warnings.append('LOG_SOURCE_UNAVAILABLE')
        database = self.db.snapshot()
        for item in database['tables'].get('runtime_health_events', []):
            rows.append(dict(event_id=f"health-{item['id']}", time=item['created_at'],
                level='WARNING' if item['state'] == 'DEGRADED' else 'INFO', module=item['component'],
                message=item['reason'], details=item['details'], source='runtime_health_events',correlation_id=None))
        for table in ('phase9_decision_status_events','execution_local_events'):
            for item in database['tables'].get(table, []):
                original = payload(item)
                rows.append(dict(event_id=str(item.get('event_id',item.get('event_key'))),
                    time=item.get('event_time',original.get('event_time')), level='UNKNOWN',
                    module='decision' if table.startswith('phase9') else 'execution',
                    message=item.get('reason_code',item.get('event_kind')), details=original,
                    source=table, correlation_id=item.get('decision_id',item.get('account_id'))))
        rows = sanitize(rows)
        filtered = [row for row in rows if (not level or row['level'] == level)
            and (not module or module.lower() in row['module'].lower())
            and (not q or q.lower() in str(row).lower())]
        filtered.sort(key=lambda row:row.get('time') or '',reverse=True)
        return envelope(dict(items=filtered[:limit], database=database['state'],
            modules=sorted({row['module'] for row in rows}), bounded=True),
            'AVAILABLE' if filtered else 'NO_DATA', sorted({row['source'] for row in filtered}),
            [*warnings,*database['warnings']])
