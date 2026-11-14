"""Start the registry-backed iOS Cloud fixture with bounded transfer recovery.

The exact-source runtime has its own provenance checks and must not call this
legacy path. Retain startup evidence before Flutter creates its test reports.
"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from time import monotonic as _monotonic, sleep


# Leave the existing fifteen-minute workflow step thirty seconds for image
# identities and final report writes. Compose up may wait for cold health checks.
STARTUP_BUDGET = 870
RETRY_DELAYS = (10, 20)
COMPOSE = ["docker", "compose", "--ansi", "never", "--progress", "plain", "-f", "docker-compose-ci.yml"]
TRANSIENT = re.compile(
    r"\bunexpected\s+eof\b|\bconnection (?:was )?reset\b|\bconnection aborted\b|"
    r"\bi/o timeout\b|\btls handshake timeout\b|\bcontext deadline exceeded\b|"
    r"\brequest timed out\b|\bclient\.timeout exceeded\b|\btimeout awaiting response headers\b|"
    r"\btemporary failure in name resolution\b|"
    r"\b(?:status code|http status)\s*[:=]?\s*(?:429|500|502|503|504)\b",
    re.IGNORECASE,
)
TERMINAL = re.compile(
    r"unauthorized|authentication required|access denied|pull access denied|"
    r"requested access .* denied|manifest unknown|not found|does not exist|no matching manifest|"
    r"no space left|disk quota exceeded|read-only file system",
    re.IGNORECASE,
)


def _redact(text: str, environment: dict) -> str:
    # GitHub's console masking does not apply to uploaded artifacts. Never emit
    # environment dumps, and mask secret values before either output destination.
    values = {
        value for key, value in environment.items()
        if value and len(value) >= 4 and re.search(r"TOKEN|PASSWORD|SECRET|KEY", key, re.IGNORECASE)
    }
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def _command(cloud: Path, output: Path, name: str, arguments: list, timeout: float, environment: dict) -> tuple:
    command = COMPOSE + arguments
    timed_out = False
    try:
        process = subprocess.Popen(
            command, cwd=cloud, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Docker invokes the Compose plugin as a child. Stop that process
            # group too, so a timed-out attempt cannot overlap its retry.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, _ = process.communicate()
            stdout += f"\nCommand timed out after {timeout} seconds.\n"
        exit_code = 124 if timed_out else process.returncode
    except OSError as error:
        stdout = f"Could not run Docker Compose: {error}\n"
        exit_code = 127
    stdout = _redact(stdout, environment)
    log_name = f"{name}.log"
    (output / log_name).write_text(stdout)
    print(stdout, end="" if stdout.endswith("\n") else "\n", flush=True)
    return {
        "command": command, "exit_code": exit_code, "timed_out": timed_out,
        "timeout_seconds": timeout, "log": log_name,
    }, stdout


def start(cloud: Path, output: Path) -> int:
    deadline = _monotonic() + STARTUP_BUDGET
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, COMPOSE_PARALLEL_LIMIT="2")
    report = {
        "stage": "pull", "pull_attempts": [], "startup": None, "exit_code": 1,
        "budget_seconds": STARTUP_BUDGET, "budget_exhausted": False,
    }

    def save() -> None:
        report["budget_exhausted"] = remaining() <= 0
        temporary = output / "startup.json.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "startup.json")

    def remaining() -> float:
        return max(0, deadline - _monotonic())

    def exhausted() -> int:
        report["exit_code"] = 124
        report["budget_exhausted"] = True
        save()
        print("Mobile Cloud startup exhausted its total time budget.", flush=True)
        return 124

    save()
    for attempt in range(1, len(RETRY_DELAYS) + 2):
        # Cold image extraction can progress for several minutes. Restarting it
        # at an arbitrary per-pull limit discards useful work; the shared budget
        # already bounds a hung command as well as transport-error retries.
        timeout = remaining()
        if timeout <= 0:
            return exhausted()
        result, stdout = _command(cloud, output, f"pull-{attempt}", ["pull"], timeout, environment)
        result["retryable"] = bool(
            result["exit_code"] and not TERMINAL.search(stdout)
            and (result["timed_out"] or TRANSIENT.search(stdout))
        )
        report["pull_attempts"].append(result)
        report["exit_code"] = result["exit_code"] or 1
        save()
        if result["exit_code"] == 0:
            break
        if not result["retryable"] or attempt > len(RETRY_DELAYS):
            return result["exit_code"]
        delay = min(RETRY_DELAYS[attempt - 1], remaining())
        if delay <= 0:
            return exhausted()
        print(f"Transient Cloud image transfer failure; retrying in {delay} seconds.", flush=True)
        sleep(delay)

    # A successful pull must be the only image acquisition phase. In particular,
    # startup must not silently fetch another mutable tag after that validation.
    report["stage"] = "start"
    report["exit_code"] = 1
    save()
    timeout = remaining()
    if timeout <= 0:
        return exhausted()
    result, _ = _command(
        cloud, output, "up", ["up", "-d", "--no-build", "--pull", "never"], timeout, environment,
    )
    report["startup"] = result
    report["exit_code"] = result["exit_code"]
    if result["exit_code"] == 0:
        if remaining() <= 0:
            return exhausted()
        report["stage"] = "started"
    save()
    return result["exit_code"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(start(arguments.cloud, arguments.output))


if __name__ == "__main__":
    main()
