"""Capture one argument-free native stack when a focused Linux test stops reporting."""

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time


IDLE_SECONDS = 180


def is_test_app(pid, app_root, proc_root=Path("/proc")):
    try:
        executable = (proc_root / str(pid) / "exe").resolve(strict=True)
        return executable.name == "AppFlowy" and executable.is_relative_to(app_root.resolve())
    except OSError:
        return False


def find_test_apps(app_root, proc_root=Path("/proc")):
    return sorted(int(path.name) for path in proc_root.iterdir()
                  if path.name.isdecimal() and is_test_app(int(path.name), app_root, proc_root))


def capture_stack(pid, app_root, output):
    debugger = shutil.which("gdb")
    if not debugger:
        return {"status": "debugger_unavailable", "pid": pid}
    if not is_test_app(pid, app_root):
        return {"status": "process_gone", "pid": pid}
    command = [
        "sudo", "-n", "timeout", "--signal=TERM", "--kill-after=5s", "15s",
        debugger, "-q", "-nx", "-batch",
        "-iex", "set auto-load off",
        "-iex", "set pagination off",
        "-iex", "set print frame-arguments none",
        "-p", str(pid), "-ex", "thread apply all bt 30", "-ex", "detach",
    ]
    status = "captured"
    code = None
    try:
        trace = output / "native-backtrace.txt"
        with trace.open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=25)
            code = result.returncode
        if code or not trace.stat().st_size:
            status = "debugger_failed"
    except subprocess.TimeoutExpired:
        status = "capture_timed_out"
    except OSError as error:
        return {"status": "debugger_unavailable", "pid": pid, "error": str(error)}
    # A terminated tracer can leave its stopped inferior awaiting continuation.
    # Revalidate ownership before releasing only the job's test application.
    if (status == "capture_timed_out" or code in (124, 137)) and is_test_app(pid, app_root):
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    return {"status": status, "pid": pid, "debugger_exit": code}


def monitor(events, app_root, output, stopped):
    output.mkdir(parents=True, exist_ok=True)
    # A replay may reuse this directory after a failed invocation.
    for name in ("native-backtrace.txt", "snapshot.json"):
        (output / name).unlink(missing_ok=True)
    previous = None
    last_progress = time.monotonic()
    result = {"status": "stopped_without_stack"}
    while not stopped.is_set():
        try:
            stat = events.stat()
            fingerprint = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            fingerprint = None
        now = time.monotonic()
        if fingerprint != previous:
            previous = fingerprint
            last_progress = now
        if now - last_progress >= IDLE_SECONDS:
            applications = find_test_apps(app_root)
            if len(applications) == 1:
                result = capture_stack(applications[0], app_root, output)
                break
            if len(applications) > 1:
                result = {"status": "ambiguous_test_processes", "pids": applications}
                break
        stopped.wait(2)
    result.update({
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "idle_seconds": IDLE_SECONDS,
        "event_file": str(events),
        "application_root": str(app_root),
    })
    (output / "snapshot.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    if not Path("/proc").is_dir():
        stopped.set()
    result = monitor(arguments.events, arguments.app_root, arguments.output, stopped)
    print("Native hang diagnostics " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
