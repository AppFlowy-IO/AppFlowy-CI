#!/usr/bin/env python3
"""Capture bounded simulator diagnostics after an Intel iOS test failure."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time


def capture(command, path, timeout=20):
    with path.open("w") as output:
        try:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False, text=True)
            return {"command": command, "exit_code": result.returncode}
        except (OSError, subprocess.SubprocessError) as error:
            output.write(f"\nDiagnostic command unavailable: {error}\n")
            return {"command": command, "error": str(error)}


def collect(device, destination, reports=None):
    if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", device):
        raise ValueError("Expected a simulator UUID")
    destination.mkdir(parents=True, exist_ok=True)
    commands = [capture(["xcrun", "simctl", "io", device, "screenshot",
                         str(destination / "simulator.png")], destination / "screenshot.log")]
    commands.append(capture([
        "xcrun", "simctl", "spawn", device, "log", "show", "--style", "compact", "--last", "1h",
        "--predicate", '(process == "AppFlowy" OR process == "Runner") AND '
        '(messageType == error OR messageType == fault)',
    ], destination / "simulator-errors.log"))
    processes = destination / "processes.log"
    commands.append(capture(["ps", "-axo", "pid=,comm="], processes))
    for line in processes.read_text().splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        pid, executable = fields
        if device in executable and executable.endswith((".app/AppFlowy", ".app/Runner")):
            # Intel debug symbols took longer than 10 s to process after the
            # sample was captured. Leave time to finish within the 2 min step.
            commands.append(capture(["sample", pid, "1", "-file", str(destination / "native-sample.txt")],
                                    destination / "sample.log", timeout=40))
            break
    reports = reports or Path.home() / "Library/Logs/DiagnosticReports"
    recent = sorted((path for path in reports.glob("*")
                     if path.is_file() and not path.is_symlink() and path.name.startswith(("AppFlowy", "Runner"))
                     and path.suffix in (".ips", ".crash") and path.stat().st_mtime >= time.time() - 7200),
                    key=lambda path: path.stat().st_mtime, reverse=True)
    for report in recent[:10]:
        if report.stat().st_size <= 10 * 1024 ** 2:
            shutil.copyfile(report, destination / report.name)
    (destination / "summary.json").write_text(json.dumps({"device": device, "commands": commands}, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    collect(args.device, args.output)


if __name__ == "__main__":
    main()
