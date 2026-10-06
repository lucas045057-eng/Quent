"""A heartbeat is current only when its actual local worker identity matches."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

from .files import FileCatalog
from .models import now, timestamp
from .security import sanitize


def process_identity(pid, root, ready_file):
    try:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 2:
            return None
        proc = Path('/proc')/str(pid)
        args = (proc/'cmdline').read_bytes().decode().strip('\0').split('\0')
        if '-m' not in args or args[args.index('-m')+1] != 'quant_nautilus.paper':
            return None
        if '--ready-file' not in args or (proc/'cwd').resolve() != root:
            return None
        declared = Path(args[args.index('--ready-file')+1])
        if (root/declared).resolve() != ready_file.resolve():
            return None
        if 'python' not in (proc/'exe').resolve().name.lower():
            return None
        stat = (proc/'stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        start_ticks = int(stat[19])
        boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
        started = datetime.fromtimestamp(boot+start_ticks/os.sysconf('SC_CLK_TCK'), timezone.utc)
        return dict(process_started_at=started.isoformat(), start_ticks=start_ticks)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


class PaperReader:
    def __init__(self, config):
        self.config = config
        self.catalog = FileCatalog(config.root)

    def snapshot(self):
        sessions, warnings = [], []
        for ref in self.catalog.scan():
            if not ref.endswith('.json'):
                continue
            try:
                report = self.catalog.read_json(ref)
            except ValueError:
                warnings.append('UNREADABLE_PAPER_ARTIFACT')
                continue
            if report.get('native_engine') != 'SandboxExecutionClient' or not isinstance(report.get('position'), dict):
                continue
            observed = timestamp(report.get('as_of'))
            age = (now()-observed).total_seconds() if observed else None
            identity = process_identity(report.get('pid'), self.config.root, self.config.root/ref)
            current = (age is not None and 0 <= age <= 5 and identity is not None
                and report.get('network_order_routes') == 0
                and observed >= timestamp(identity['process_started_at']))
            session = sanitize(dict(session_id=hashlib.sha256(ref.encode()).hexdigest()[:24],
                source=ref, heartbeat_at=report.get('as_of'), heartbeat_age_seconds=age,
                freshness='CURRENT' if current else 'STALE', state='RUNNING' if current else 'STOPPED',
                pid=report.get('pid'), process_started_at=identity['process_started_at'] if identity else None,
                uptime_seconds=(now()-timestamp(identity['process_started_at'])).total_seconds() if identity else None,
                last_restart_at=None, restored=report.get('restored'),
                acceptance_kind=report.get('acceptance_kind'), reconciliation=report.get('state'),
                position=report['position'], order_states=report.get('order_states', {}),
                repair_count=report.get('repair_count'), rss_bytes=report.get('rss_bytes'),
                peak_rss_bytes=report.get('peak_rss_bytes'), network_order_routes=report.get('network_order_routes'),
                funding_payment_count=len(report['funding_keys']) if isinstance(report.get('funding_keys'), list) else None))
            sessions.append(session)
        sessions.sort(key=lambda item: item.get('heartbeat_at') or '', reverse=True)
        sessions = sessions[:200]
        currents = [session for session in sessions if session['freshness'] == 'CURRENT']
        return dict(state='RUNNING' if currents else ('STOPPED' if sessions else 'NO_DATA'),
            current=currents[0] if len(currents) == 1 else None, currents=currents,
            sessions=sessions, warnings=sorted(set(warnings)))
