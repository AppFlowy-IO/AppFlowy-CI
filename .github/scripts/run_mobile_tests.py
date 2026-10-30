"""Bound a CI-owned Flutter test process independently of its device isolate."""

import argparse
import codecs
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time


CASE = re.compile(r"^\d+:\d+(?::\d+)? (?:[+~!\-]\d+\s*)+: (.+)$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def stop_process_group(process, grace_seconds):
    # Flutter's helpers share the new session. A device-isolate hang must not
    # leave a host test process running after this step has failed.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            continue
        # A helper may outlive its parent, so finish retiring this owned group.
        if sig != signal.SIGKILL:
            continue
        break
    process.wait()


def run_bounded(command, *, build_seconds, case_seconds, report_path,
                output=None, on_timeout=None, grace_seconds=5):
    if min(build_seconds, case_seconds, grace_seconds) <= 0:
        raise ValueError("Build, case and termination deadlines must be positive")
    report_path = Path(report_path)
    if report_path.exists():
        raise FileExistsError(report_path)
    output = output or sys.stdout
    started = time.monotonic()
    case_started = None
    current_case = None
    cases = []
    pending = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    timed_out = False
    try:
        while process.poll() is None or selector.get_map():
            now = time.monotonic()
            deadline = (started + build_seconds if case_started is None
                        else case_started + case_seconds)
            if now >= deadline:
                timed_out = True
                if on_timeout is not None:
                    on_timeout()
                break
            for key, _ in selector.select(timeout=min(0.2, deadline - now)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                decoded = decoder.decode(chunk)
                output.write(decoded)
                output.flush()
                pending += decoded
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    match = CASE.match(ANSI.sub("", line).rstrip("\r"))
                    if match is None or match[1].startswith("loading "):
                        continue
                    name = match[1]
                    if name != current_case:
                        current_case = name
                        case_started = time.monotonic()
                        cases.append(name)
                # Preserve bounded parser memory even if a process emits a long
                # incomplete line. Forwarded output itself remains unchanged.
                pending = pending[-65536:]
        if timed_out:
            output.write(f"\n::error::Mobile test exceeded its {'build' if current_case is None else 'case'} deadline: {current_case or 'application startup'}\n")
            output.flush()
            stop_process_group(process, grace_seconds)
            returncode = 124
        else:
            returncode = process.wait()
        report = {"returncode": returncode, "timed_out": timed_out,
                  "phase": "build" if current_case is None else "case",
                  "current_case": current_case, "started_cases": cases,
                  "elapsed_seconds": round(time.monotonic() - started, 3),
                  "build_limit_seconds": build_seconds, "case_limit_seconds": case_seconds}
        report_path.write_text(json.dumps(report, indent=2))
        return returncode
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            stop_process_group(process, grace_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("This command is for a job-owned CI simulator")
    if re.fullmatch(r"[0-9a-fA-F-]{36}", args.simulator) is None:
        parser.error("Expected the selected simulator UUID")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    if not args.report.resolve().is_relative_to(temporary):
        parser.error("The diagnostic report must belong to RUNNER_TEMP")

    def capture_timeout():
        try:
            subprocess.run(["xcrun", "simctl", "io", args.simulator, "screenshot",
                            str(temporary / "mobile-local-timeout.png")],
                           capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass

    return run_bounded(
        ["flutter", "test", "integration_test/mobile_runner_4.dart", "-d", args.simulator,
         "--reporter", "expanded"],
        build_seconds=40 * 60, case_seconds=8 * 60, report_path=args.report,
        on_timeout=capture_timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
