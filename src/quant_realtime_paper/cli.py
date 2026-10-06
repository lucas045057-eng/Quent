"""CLI for starting, supervising, and inspecting the realtime paper monitor."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .acceptance import evaluate_formal_acceptance
from .config import RuntimeConfig
from .runtime import RealtimePaperMonitor
from .store import SessionStore


def parse_duration(value: str) -> float:
    raw = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(raw) < 2 or raw[-1] not in units:
        raise argparse.ArgumentTypeError("duration must use seconds, minutes, hours, or days (e.g. 15m, 24h, 72h, 7d)")
    try:
        amount = float(raw[:-1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration amount must be positive") from exc
    seconds = amount * units[raw[-1]]
    if seconds <= 0 or seconds > 7 * 86400:
        raise argparse.ArgumentTypeError("duration must be greater than zero and no more than 7d")
    return seconds


def _state_directory(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    return RuntimeConfig.from_env().state_db.parent


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def start_worker(duration: str | None, state_dir: Path) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_path = state_dir / "runner.pid"
    if pid_path.is_file():
        try:
            existing = int(pid_path.read_text(encoding="ascii").strip())
        except (ValueError, OSError):
            existing = 0
        if existing > 0 and _pid_alive(existing):
            print(json.dumps({"state": "ALREADY_RUNNING", "pid": existing}))
            return 0
        pid_path.unlink(missing_ok=True)
    command = [sys.executable, "-m", "quant_realtime_paper", "run", "--state-dir", str(state_dir)]
    if duration:
        command.extend(["--duration", duration])
    log_path = state_dir / "runner.log"
    log_handle = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(command, cwd=RuntimeConfig.from_env().project_root,
                               stdin=subprocess.DEVNULL, stdout=log_handle,
                               stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    pid_path.write_text(str(process.pid), encoding="ascii")
    log_handle.close()
    print(json.dumps({"state": "STARTING", "pid": process.pid, "log": str(log_path)}))
    return 0


def stop_worker(state_dir: Path) -> int:
    pid_path = state_dir / "runner.pid"
    if not pid_path.is_file():
        print(json.dumps({"state": "STOPPED"}))
        return 0
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        print(json.dumps({"state": "PID_FILE_INVALID"}))
        return 2
    if not _pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        print(json.dumps({"state": "STOPPED"}))
        return 0
    try:
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        print(json.dumps({"state": "PROCESS_NOT_VERIFIABLE"}))
        return 2
    if b"quant_realtime_paper" not in command_line:
        print(json.dumps({"state": "PID_NOT_OWNED"}))
        return 2
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not _pid_alive(pid):
            pid_path.unlink(missing_ok=True)
            print(json.dumps({"state": "STOPPED", "pid": pid}))
            return 0
        time.sleep(0.1)
    print(json.dumps({"state": "STOP_REQUESTED", "pid": pid}))
    return 0


def show_status(state_dir: Path) -> int:
    snapshot = SessionStore.readonly_snapshot(state_dir / "sessions.sqlite3")
    pid = None
    running = False
    pid_path = state_dir / "runner.pid"
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
            running = _pid_alive(pid)
        except (ValueError, OSError):
            pass
    print(json.dumps({"process_running": running, "pid": pid, **snapshot}, sort_keys=True, default=str))
    return 0


def show_acceptance(state_dir: Path, *, minimum_duration_seconds: float, session_id: str | None) -> int:
    store = SessionStore(state_dir / "sessions.sqlite3")
    if session_id:
        session = store.session(session_id)
    else:
        snapshot = SessionStore.readonly_snapshot(state_dir / "sessions.sqlite3")
        session = snapshot.get("session")
    if session:
        cycles = store.cycle_history(session["session_id"])
        decisions = store.decision_sources(session["session_id"])
    else:
        cycles, decisions = [], []
    report = evaluate_formal_acceptance(
        session, cycles, decisions, minimum_duration_seconds=minimum_duration_seconds,
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant-realtime-paper")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run in the foreground")
    run_parser.add_argument("--duration", type=parse_duration)
    run_parser.add_argument("--state-dir")
    start_parser = sub.add_parser("start", help="start a detached monitor process")
    start_parser.add_argument("--duration")
    start_parser.add_argument("--state-dir")
    stop_parser = sub.add_parser("stop", help="stop only this monitor process")
    stop_parser.add_argument("--state-dir")
    status_parser = sub.add_parser("status", help="show the latest persisted session status")
    status_parser.add_argument("--state-dir")
    acceptance_parser = sub.add_parser("acceptance", help="verify a completed long-run session")
    acceptance_parser.add_argument("--duration", type=parse_duration, default=parse_duration("24h"))
    acceptance_parser.add_argument("--session-id")
    acceptance_parser.add_argument("--state-dir")
    args = parser.parse_args(argv)
    state_dir = _state_directory(args.state_dir)
    if args.command == "start":
        if args.duration:
            try:
                parse_duration(args.duration)
            except argparse.ArgumentTypeError as exc:
                parser.error(str(exc))
        return start_worker(args.duration, state_dir)
    if args.command == "stop":
        return stop_worker(state_dir)
    if args.command == "status":
        return show_status(state_dir)

    if args.command == "acceptance":
        return show_acceptance(state_dir, minimum_duration_seconds=args.duration,
                               session_id=args.session_id)
    config = RuntimeConfig.from_env()
    if args.state_dir:
        from dataclasses import replace
        config = replace(config, state_db=Path(args.state_dir).resolve() / "sessions.sqlite3")
    from .assembly import build_default_runtime
    monitor = build_default_runtime(config, environ=os.environ)
    stop_event = __import__("threading").Event()
    def stop_signal(*_):
        stop_event.set()
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    pid_path = config.state_db.parent / "runner.pid"
    config.state_db.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    try:
        monitor.run(duration_seconds=args.duration, stop_event=stop_event)
    finally:
        pid_path.unlink(missing_ok=True)
        close = getattr(monitor, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
