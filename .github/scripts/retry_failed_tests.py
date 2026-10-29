#!/usr/bin/env python3
"""Run a Rust test command, then rerun only the tests that failed.

For lanes whose AI is a small local model, which misses a graded answer now and then. A test
fails the run only when it fails every attempt; tests that pass on a rerun are reported as a
warning. Nothing is rerun unless the output accounts for every failure, so a crashed test
binary, a build error or a failure libtest did not name still fails at once.

Usage: retry_failed_tests.py [--attempts N] -- <cargo test or cloud_test_binaries.py run ...>
"""

import argparse
import re
import subprocess
import sys


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# cargo's "Running"/"Doc-tests" lines, and cloud_test_binaries.py's "Running precompiled".
BINARY_START = re.compile(
    r"^(?:\s+Running \S.* \(\S+\)|\s+Doc-tests \S+|Running precompiled \S+ \(\S+\))$"
)
TEST_OUTCOME = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)$")
RESULT = re.compile(r"^test result: (?:ok|FAILED)\. \d+ passed; (\d+) failed;")


def run(command):
    """Run the command, echoing its output as it arrives; return its status and lines."""
    lines = []
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    ) as process:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(ANSI_ESCAPE.sub("", line.rstrip("\r\n")))
    return process.returncode, lines


def outcomes(lines):
    """Map each reported test to its outcome, or None when a failure is unaccounted for.

    Every test binary that started must have reported a result, and the tests named as
    failed must add up to the failures those results count.
    """
    reported = {}
    counted_failures = 0
    awaiting_result = False
    for line in lines:
        if BINARY_START.match(line):
            if awaiting_result:
                return None
            awaiting_result = True
        elif match := RESULT.match(line):
            awaiting_result = False
            counted_failures += int(match.group(1))
        elif match := TEST_OUTCOME.match(line):
            name, outcome = match.groups()
            if reported.get(name) != "FAILED":
                reported[name] = outcome
    named_failures = sum(outcome == "FAILED" for outcome in reported.values())
    if awaiting_result or named_failures != counted_failures:
        return None
    return reported


def rerun_command(command, names):
    """The command with libtest told to run exactly the named tests."""
    separator = [] if "--" in command else ["--"]
    return command + separator + ["--exact", *names]


def failure_status(status):
    return status if status > 0 else 1


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    # The command keeps its own "--" for libtest, so split at the first one by hand.
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=3, help="Runs per failed test, the first included")
    args = parser.parse_args(arguments[:separator])
    command = arguments[separator + 1:]
    if not command or args.attempts < 1:
        parser.error("a test command after -- and at least one attempt are required")

    status, lines = run(command)
    if status == 0:
        return 0
    reported = outcomes(lines)
    pending = [name for name, outcome in (reported or {}).items() if outcome == "FAILED"]
    if not pending:
        print("::error title=Not rerun::The test command failed without naming every failure "
              "(a crashed test binary or a build error), so nothing was rerun", flush=True)
        return failure_status(status)

    passed_on_rerun = []
    for attempt in range(2, args.attempts + 1):
        print(f"==> Attempt {attempt} of {args.attempts}: rerunning {len(pending)} failed "
              f"test(s): {' '.join(pending)}", flush=True)
        status, lines = run(rerun_command(command, pending))
        reported = outcomes(lines)
        if reported is None or any(name not in reported for name in pending):
            print("::error title=Rerun incomplete::The rerun did not report a result for every "
                  "test it was asked to run", flush=True)
            return failure_status(status)
        passed_on_rerun += [name for name in pending if reported[name] == "ok"]
        pending = [name for name in pending if reported[name] != "ok"]
        if not pending:
            break

    if passed_on_rerun:
        print(f"::warning title=Passed on a rerun::{len(passed_on_rerun)} test(s) failed, then "
              f"passed on a rerun: {', '.join(passed_on_rerun)}", flush=True)
    if pending:
        print(f"::error title=Failed every attempt::{len(pending)} test(s) failed all "
              f"{args.attempts} attempts: {', '.join(pending)}", flush=True)
        return failure_status(status)
    if status != 0:
        print(f"::error title=Rerun failed::Every rerun test passed, but the rerun exited "
              f"with status {status}", flush=True)
        return failure_status(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
