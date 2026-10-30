#!/usr/bin/env python3
"""Run the complete Desktop PDF target and retain its inventory and saved files.

Libtest's substring filters match test names, not target filenames. This gate
intentionally passes no filter. A source-owned contract keeps critical cases
from disappearing or becoming ignored while older Desktop refs remain usable.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


PACKAGE = "event-integration-test"
TARGET = "database_pdf_export"
CONTRACT = Path("integration-test/pdf_export_contract.json")
TEST_SOURCE = Path("integration-test/tests/database_pdf_export.rs")


def inventory(text: str) -> set[str]:
    return set(re.findall(r"^([A-Za-z_][A-Za-z0-9_:]*): test$", text, re.MULTILINE))


def desktop_revision(workspace: Path) -> str:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Desktop checkout must resolve to an immutable SHA")
    return sha


def verify_result(text: str, expected: int) -> None:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    summaries = re.findall(
        r"^test result: ok\. (\d+) passed; (\d+) failed; \d+ ignored; \d+ measured; (\d+) filtered out;",
        text, re.MULTILINE,
    )
    if len(summaries) != 1 or tuple(map(int, summaries[0])) != (expected, 0, 0):
        raise ValueError(f"PDF run must execute all {expected} non-ignored tests without filtering")


def required_tests(workspace: Path) -> list[str]:
    contract = workspace / CONTRACT
    if not contract.exists():
        return []
    data = json.loads(contract.read_text())
    if (data.get("version"), data.get("package"), data.get("target")) != (1, PACKAGE, TARGET):
        raise ValueError("Unsupported Desktop PDF contract version/package/target")
    names = data.get("required_tests")
    if not isinstance(names, list) or not names or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:]*", name)
        for name in names
    ) or len(names) != len(set(names)):
        raise ValueError("PDF contract must contain unique required test names")
    return names


def run(workspace: Path, output: Path) -> int:
    workspace, output = workspace.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "gate-result.json").unlink(missing_ok=True)
    result = {"kind": "rust", "package": PACKAGE, "target": TARGET, "status": "failed"}
    try:
        required = required_tests(workspace)
        if not (workspace / TEST_SOURCE).exists():
            if (workspace / CONTRACT).exists():
                raise ValueError("Desktop PDF contract requires missing database_pdf_export target")
            result.update(status="legacy_skipped", reason="Desktop ref predates the PDF target")
            print(result["reason"])
            return 0

        result["desktop_sha"] = desktop_revision(workspace)
        command = ["cargo", "test", "-p", PACKAGE, "--test", TARGET]
        environment = {**os.environ, "DISABLE_CI_TEST_LOG": "true",
                       "APPFLOWY_PDF_TEST_OUTPUT_DIR": str(output / "saved-exports")}
        (output / "saved-exports").mkdir(exist_ok=True)
        lists = {}
        for label, flags in (("all", []), ("ignored", ["--ignored"])):
            listed = subprocess.run(command + ["--", "--list", *flags], cwd=workspace,
                                    env=environment, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, check=False)
            (output / f"inventory-{label}.log").write_text(listed.stdout)
            if listed.returncode:
                raise ValueError(f"PDF {label} inventory failed ({listed.returncode})")
            lists[label] = inventory(listed.stdout)
        runnable = lists["all"] - lists["ignored"]
        result.update(required_tests=required, tests=sorted(lists["all"]),
                      runnable_count=len(runnable),
                      ignored_tests=sorted(lists["ignored"]))
        if not runnable:
            raise ValueError("PDF target has no non-ignored tests")
        missing = set(required) - lists["all"]
        ignored = set(required) & lists["ignored"]
        if missing or ignored:
            raise ValueError(f"PDF contract not satisfied: missing={sorted(missing)}, ignored={sorted(ignored)}")

        command += ["--", "--test-threads=1"]
        result["command"] = command
        print(f"Running all {len(runnable)} non-ignored PDF tests without a name filter", flush=True)
        with (output / "test.log").open("w") as log:
            completed = subprocess.run(command, cwd=workspace, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT, check=False)
        result["exit_code"] = completed.returncode
        if completed.returncode == 0:
            verify_result((output / "test.log").read_text(errors="replace"), len(runnable))
            result["status"] = "passed"
        print(f"PDF target exit code: {completed.returncode}; evidence: {output}")
        return completed.returncode
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        print(f"Desktop PDF gate failed: {error}", file=sys.stderr)
        return 1
    finally:
        (output / "gate-result.json").write_text(json.dumps(result, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    return run(args.workspace, args.output)


if __name__ == "__main__":
    sys.exit(main())
