"""One late, symbols-only sample of this Linux Flutter step's native app."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


DELAY_SECONDS = 40 * 60  # The workflow enforces a 45-minute step timeout.
MAX_THREADS = 96
DEBUGGER_SECONDS = 15
REPORT_PREFIX = "AF_NATIVE_STACK "
# GDB's ordinary backtrace may include argument values, file paths and source
# lines. Read only thread IDs, function symbols and shared-library basenames.
GDB_FRAMES = r"""
import gdb, json, os, re
def target_matches():
    image = os.stat('/proc/%d/exe' % expected_pid)
    with open('/proc/%d/stat' % expected_pid) as stat:
        start_ticks = int(stat.read().rsplit(') ', 1)[1].split()[19])
    return (image.st_dev, image.st_ino, start_ticks) == expected_identity

if target_matches():
    gdb.execute('attach %d' % expected_pid, to_string=True)
inferior = gdb.selected_inferior()
if inferior.pid == expected_pid and target_matches():
    mappings = []
    with open('/proc/%d/maps' % expected_pid) as source:
        for line in source:
            fields = line.split(None, 5)
            if len(fields) != 6 or 'x' not in fields[1]:
                continue
            module = os.path.basename(fields[5].strip())
            if module == 'AppFlowy' or re.fullmatch(r'lib[\w.+-]+\.so(?:\.[0-9]+)*', module):
                start, end = (int(n, 16) for n in fields[0].split('-'))
                mappings.append((start, end, int(fields[2], 16), module))
    for thread in sorted(inferior.threads(), key=lambda t: t.ptid[1])[:max_threads]:
        frames = []
        try:
            thread.switch()
            frame = gdb.newest_frame()
            while frame and len(frames) < 20:
                symbol = (frame.name() or '??').split('(', 1)[0][:160]
                entry = {'symbol': symbol if re.fullmatch(r'[\w :~<>{}.,$+*&?-]+', symbol) else '??'}
                pc = frame.pc()
                for start, end, offset, module in mappings:
                    if start <= pc < end:
                        entry.update(module=module, file_offset=hex(pc - start + offset))
                        break
                frames.append(entry)
                frame = frame.older()
        except gdb.error:
            pass
        print('AF_NATIVE_STACK ' + json.dumps([thread.ptid[1], frames]), flush=True)
"""


def descendants(root):
    # Linux's children file is per thread. Flutter can launch the native app
    # from a worker rather than from the thread that owns its process ID.
    pending = [root]
    seen = {root}
    while pending:
        pid = pending.pop()
        for children in Path(f"/proc/{pid}/task").glob("*/children"):
            try:
                pids = children.read_text().split()
            except OSError:
                continue
            for child in map(int, pids):
                if child not in seen:
                    seen.add(child)
                    pending.append(child)
                    yield child


def is_app(pid, executable):
    try:
        return executable.samefile(f"/proc/{pid}/exe")
    except OSError:
        return False


def wait_states(pid):
    threads = []
    tasks = Path(f"/proc/{pid}/task")
    for task in sorted(tasks.glob("[0-9]*"), key=lambda path: int(path.name))[:MAX_THREADS]:
        try:
            state = (task / "stat").read_text().rsplit(") ", 1)[1][0]
            wait = (task / "wchan").read_text().strip()
        except (OSError, IndexError):
            continue
        threads.append({"tid": int(task.name), "state": state,
                        "wait": wait if re.fullmatch(r"[A-Za-z0-9_]+", wait) else None})
    return threads


def stack_sample(pid, executable, debugger, use_sudo):
    if not debugger.is_file():
        return {"status": "gdb unavailable", "threads": []}
    try:
        image = executable.stat()
        start_ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19])
    except (OSError, IndexError):
        return {"status": "target exited", "threads": []}
    identity = image.st_dev, image.st_ino, start_ticks
    # Hosted Ubuntu restricts sibling ptrace. Keep its global policy intact.
    # No sudo prompt or external symbols. timeout also bounds an elevated GDB
    # if the runner cancels its unprivileged Python parent during attachment.
    command = ["/usr/bin/timeout", "--kill-after=2", str(DEBUGGER_SECONDS), str(debugger),
               "-nx", "-nh", "-q", "-batch", "-iex", "set auto-load off",
               "-iex", "set debuginfod enabled off", "-iex", "set print thread-events off",
               "-iex", "set print frame-arguments none", "-iex", "set print entry-values no",
               "-ex", f"python expected_pid={pid}; expected_identity={identity}; max_threads={MAX_THREADS}",
               "-ex", f"python exec({GDB_FRAMES!r})", "-ex", "detach"]
    if use_sudo:
        command = ["/usr/bin/sudo", "-n", "--", *command]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=20, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C",
                                                 "DEBUGINFOD_URLS": ""})
        output = result.stdout
        timed_out = result.returncode in (124, 137)
    except OSError:
        timed_out = False
        output = b""
    except subprocess.TimeoutExpired as error:
        timed_out = True
        output = error.stdout or b""
    threads = []
    for line in output.decode(errors="replace").splitlines():
        if line.startswith(REPORT_PREFIX):
            # timeout can interrupt an individual line. Keep completed threads.
            try:
                tid, frames = json.loads(line[len(REPORT_PREFIX):])
            except (ValueError, TypeError):
                continue
            threads.append({"tid": tid, "frames": frames})
    status = "debugger timed out" if timed_out else "attach unavailable"
    if threads and not timed_out:
        status = "captured"
    return {"status": status, "threads": threads}


def write_report(path, report):
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)


def watch(root, executable, output, delay=DELAY_SECONDS, debugger=Path("/usr/bin/gdb"), use_sudo=False):
    # An open procfs file identifies the original shell even if its PID is
    # reused. Stop promptly when setup, the test, or the shell itself exits.
    try:
        step = open(f"/proc/{root}/stat")
    except FileNotFoundError:
        return
    deadline = time.monotonic() + delay
    with step:
        while True:
            try:
                step.seek(0)
                if step.read().rsplit(") ", 1)[1][0] in "ZX":
                    return
            except (OSError, IndexError):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1, remaining))
        matches = [pid for pid in descendants(root) if is_app(pid, executable)]
        report = {"matching_processes": len(matches), "apps": []}
        # If a test left an older app alive, include both the newest process and
        # one predecessor. Record the kernel fallback before attempting ptrace.
        for pid in sorted(matches, reverse=True)[:2]:
            report["apps"].append({"pid": pid, "wait_states": wait_states(pid)})
        write_report(output, report)
        for app in report["apps"]:
            app.update(stack_sample(app["pid"], executable, debugger, use_sudo))
            write_report(output, report)


if __name__ == "__main__":
    watch(int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), use_sudo=True)
