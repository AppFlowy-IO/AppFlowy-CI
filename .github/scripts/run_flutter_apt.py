"""Bound downloads on disposable runners without interrupting package installation."""

import os
import subprocess
import sys


def run_apt(arguments, run=subprocess.run):
    operation = next((value for value in arguments if not value.startswith("-")), None)
    if operation not in ("install", "update"):
        raise ValueError("Only apt-get install and update are supported")
    install = operation == "install"
    download = ["apt-get", *(["--download-only"] if install else []), *arguments]
    for attempt in range(2):
        result = run([
            "sudo", "timeout", "--signal=TERM", "--kill-after=10s", "120s",
            *download,
        ])
        if result.returncode == 0:
            break
        if result.returncode != 124 or attempt == 1:
            return result.returncode
        # APT retains completed archives and resumable partial downloads. Its
        # socket timeout does not bound a server that keeps trickling bytes.
        print("Package download exceeded 120 seconds; resuming once.", flush=True)
    if install:
        # Never terminate dpkg mid-transaction or let this phase fetch more data.
        return run(["sudo", "apt-get", "--no-download", *arguments]).returncode
    return 0


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        raise SystemExit("Flutter APT installation is only for GitHub Linux runners")
    raise SystemExit(run_apt(sys.argv[1:]))
