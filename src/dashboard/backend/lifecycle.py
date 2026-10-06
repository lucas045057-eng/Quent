"""Own only the dashboard PID; process identity and pidfd protect against reuse."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4


def verify_bundle(root):
    frontend = Path(root)/'dashboard/frontend'
    stamp_path = frontend/'dist/build-stamp.json'
    try:
        if stamp_path.is_symlink() or stamp_path.stat().st_size > 1048576:
            raise ValueError()
        stamp = json.loads(stamp_path.read_text())
        if stamp['schema'] != 'QUANT_DASHBOARD_BUILD_V1':
            raise ValueError()
        for group, code in (('source_files','BUNDLE_SOURCE_MISMATCH'),('assets','BUNDLE_ASSET_MISMATCH')):
            hashes = stamp[group]
            if not isinstance(hashes,dict) or not hashes or len(hashes) > 512:
                raise ValueError()
            for ref, expected in hashes.items():
                target = frontend/ref
                if not target.resolve().is_relative_to(frontend.resolve()) or '..' in Path(ref).parts or Path(ref).is_absolute():
                    raise ValueError()
                if any(path.is_symlink() for path in (target,*target.parents) if path != frontend.parent):
                    raise ValueError()
                if not target.is_file() or target.stat().st_size > 4194304:
                    raise ValueError(code)
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    raise ValueError(code)
        return stamp
    except (OSError,KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('BUNDLE_MISSING_OR_INVALID') from None


def _directory(config):
    directory = config.root/'artifacts/dashboard'
    if (config.root/'artifacts').is_symlink() or directory.is_symlink():
        raise ValueError('UNSAFE_RUNTIME_DIRECTORY')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.chmod(0o700)
    return directory


@contextmanager
def _lock(config):
    target = _directory(config)/f'service-{config.port}.lock'
    with os.fdopen(os.open(target,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600),'a') as stream:
        try:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('DASHBOARD_OPERATION_IN_PROGRESS') from None
        try:
            yield
        finally:
            fcntl.flock(stream,fcntl.LOCK_UN)


def _record(config):
    target = _directory(config)/f'service-{config.port}.json'
    try:
        with os.fdopen(os.open(target,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return None
        value = json.loads(raw)
        return value if isinstance(value,dict) else None
    except (OSError,ValueError):
        return None


def _ticks(pid):
    parts = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
    return None if parts[0] == 'Z' else int(parts[19])


def _owned(config,record):
    try:
        pid = record['pid']
        if not isinstance(pid,int) or isinstance(pid,bool) or pid < 2 or record['root'] != str(config.root) or record['port'] != config.port:
            return False
        if _ticks(pid) != record['start_ticks']:
            return False
        proc = Path('/proc')/str(pid)
        args = (proc/'cmdline').read_bytes().decode().strip('\0').split('\0')
        expected = [str(sys.executable),'-m','dashboard.backend','serve','--root',str(config.root),
            '--port',str(config.port),'--instance',record['instance_id']]
        return (args == expected and (proc/'cwd').resolve() == config.root
            and (proc/'exe').resolve() == Path(sys.executable).resolve())
    except (KeyError,TypeError,OSError,ValueError,IndexError):
        return False


def status(config):
    record = _record(config)
    if record and _owned(config,record):
        return dict(state='RUNNING',pid=record['pid'],instance_id=record['instance_id'],
            url=f'http://127.0.0.1:{config.port}')
    return dict(state='STOPPED',pid=None,url=f'http://127.0.0.1:{config.port}')


def start(config, *, hold=False):
    child = None
    with _lock(config):
        existing = status(config)
        if existing['state'] == 'RUNNING':
            return {**existing,'already_running':True}
        verify_bundle(config.root)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            try:
                probe.bind(('127.0.0.1',config.port))
            except OSError:
                raise ValueError('PORT_IN_USE') from None
        instance = str(uuid4())
        command = [str(sys.executable),'-m','dashboard.backend','serve','--root',str(config.root),
            '--port',str(config.port),'--instance',instance]
        env = dict(os.environ,QUANT_DASHBOARD_ROOT=str(config.root),QUANT_DASHBOARD_PORT=str(config.port))
        log = _directory(config)/f'service-{config.port}.log'
        with os.fdopen(os.open(log,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600),'w') as stream:
            child = subprocess.Popen(command,cwd=config.root,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        record = dict(pid=child.pid,start_ticks=_ticks(child.pid),instance_id=instance,root=str(config.root),port=config.port)
        destination = _directory(config)/f'service-{config.port}.json'
        temporary = _directory(config)/f'service-{config.port}-{instance}.tmp'
        try:
            deadline = time.monotonic()+12
            ready = False
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    with urlopen(f'http://127.0.0.1:{config.port}/api/health',timeout=2) as response:
                        body = json.load(response)
                        ready = body.get('schema') == 'DASHBOARD_API_V1' and body.get('data',{}).get('process_pid') == child.pid
                    if ready:
                        break
                except (OSError,ValueError,TimeoutError):
                    pass
                time.sleep(.1)
            if not ready or not _owned(config,record):
                raise ValueError('DASHBOARD_START_FAILED')
            with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as stream:
                json.dump(record,stream)
            temporary.replace(destination)
        except BaseException:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            temporary.unlink(missing_ok=True)
            raise
        result = {**status(config),'already_running':False}
    if hold:
        print(json.dumps(result),flush=True)
        try:
            child.wait()
        except KeyboardInterrupt:
            stop(config)
        return dict(state='STOPPED',pid=None,url=result['url'])
    return result


def stop(config):
    with _lock(config):
        record = _record(config)
        if not record:
            return dict(state='STOPPED',pid=None)
        if not _owned(config,record):
            try:
                alive = _ticks(record.get('pid')) is not None
            except (OSError,ValueError,IndexError):
                alive = False
            return dict(state='NOT_OWNED' if alive else 'STOPPED',pid=None)
        descriptor = os.pidfd_open(record['pid'])
        try:
            # Re-check AFTER opening pidfd: never signal an identity seen before PID reuse.
            if not _owned(config,record):
                return dict(state='NOT_OWNED',pid=None)
            signal.pidfd_send_signal(descriptor,signal.SIGTERM)
            exited = select.select([descriptor],[],[],5)[0]
            if not exited:
                signal.pidfd_send_signal(descriptor,signal.SIGKILL)
                select.select([descriptor],[],[],2)
        finally:
            os.close(descriptor)
        (_directory(config)/f'service-{config.port}.json').unlink(missing_ok=True)
        return dict(state='STOPPED',pid=None)
