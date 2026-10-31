#!/usr/bin/env python3
"""Run complete Desktop PDF event/component targets and retain saved-file evidence.

Libtest's substring filters match test names, not target filenames. This gate
intentionally passes no filter. A source-owned contract keeps critical cases
from disappearing or becoming ignored while older Desktop refs remain usable.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
COMPONENT_SOURCE = Path("flowy-database2/tests/pdf_export_columns.rs")


@dataclass(frozen=True)
class Suite:
    package: str
    target: str
    source: Path
    required: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.package}-{self.target}"

    @property
    def command(self) -> list[str]:
        return ["cargo", "test", "-p", self.package, "--test", self.target]


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


def required_names(data: dict) -> tuple[str, ...]:
    names = data.get("required_tests")
    if not isinstance(names, list) or not names or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:]*", name)
        for name in names
    ) or len(names) != len(set(names)):
        raise ValueError("PDF contract must contain unique required test names")
    return tuple(names)


def suites(workspace: Path) -> list[Suite]:
    contract = workspace / CONTRACT
    if not contract.exists():
        if not (workspace / TEST_SOURCE).exists():
            return []
        selected = [Suite(PACKAGE, TARGET, TEST_SOURCE)]
        if (workspace / COMPONENT_SOURCE).exists():
            selected.append(Suite("flowy-database2", "pdf_export_columns", COMPONENT_SOURCE))
        return selected
    data = json.loads(contract.read_text())
    if (data.get("version"), data.get("package"), data.get("target")) != (1, PACKAGE, TARGET):
        raise ValueError("Unsupported Desktop PDF contract version/package/target")
    components = data.get("components")
    if not isinstance(components, dict) or (components.get("package"), components.get("target")) != (
        "flowy-database2", "pdf_export_columns"
    ):
        raise ValueError("Desktop PDF contract requires its pdf_export_columns component target")
    selected = [Suite(PACKAGE, TARGET, TEST_SOURCE, required_names(data)),
                Suite("flowy-database2", "pdf_export_columns", COMPONENT_SOURCE, required_names(components))]
    for suite in selected:
        if not (workspace / suite.source).exists():
            raise ValueError(f"Desktop PDF contract requires missing {suite.target} target")
    return selected


def run_suite(suite: Suite, workspace: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for name in ("result.json", "test.log", "inventory-all.log", "inventory-ignored.log"):
        (output / name).unlink(missing_ok=True)
    result = {"package": suite.package, "target": suite.target, "status": "failed",
              "exit_code": 1, "required_tests": list(suite.required)}
    try:
        command = suite.command
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
        result.update(tests=sorted(lists["all"]), runnable_count=len(runnable),
                      ignored_tests=sorted(lists["ignored"]))
        if not runnable:
            raise ValueError("PDF target has no non-ignored tests")
        missing = set(suite.required) - lists["all"]
        ignored = set(suite.required) & lists["ignored"]
        if missing or ignored:
            raise ValueError(f"PDF contract not satisfied: missing={sorted(missing)}, ignored={sorted(ignored)}")
        command += ["--", "--test-threads=1"]
        result["command"] = command
        print(f"Running all {len(runnable)} non-ignored tests in {suite.label} without a filter", flush=True)
        with (output / "test.log").open("w") as log:
            completed = subprocess.run(command, cwd=workspace, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT, check=False)
        result["exit_code"] = completed.returncode
        if completed.returncode == 0:
            verify_result((output / "test.log").read_text(errors="replace"), len(runnable))
            result["status"] = "passed"
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        result.update(error=str(error), exit_code=1)
        print(f"Desktop PDF gate failed for {suite.label}: {error}", file=sys.stderr)
    finally:
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def build(workspace: Path, output: Path) -> int:
    """Compile the same source-owned targets before starting their execution budget."""
    workspace, output = workspace.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # A successful build must never be mistaken for an executed regression gate.
    (output / "gate-result.json").unlink(missing_ok=True)
    result = {"kind": "rust_build", "status": "failed", "exit_code": 1, "suites": []}
    try:
        selected = suites(workspace)
        result["desktop_sha"] = desktop_revision(workspace)
        for suite in selected:
            suite_output = output / suite.label
            suite_output.mkdir(exist_ok=True)
            command = suite.command + ["--no-run"]
            print(f"Building {suite.label} before the PDF execution gate", flush=True)
            with (suite_output / "build.log").open("w") as log:
                completed = subprocess.run(command, cwd=workspace,
                                           stdout=log, stderr=subprocess.STDOUT, check=False)
            result["suites"].append({"package": suite.package, "target": suite.target,
                                    "command": command, "exit_code": completed.returncode})
            if completed.returncode:
                result["exit_code"] = completed.returncode
                return completed.returncode
        result.update(status="built" if selected else "legacy_skipped", exit_code=0)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        print(f"Desktop PDF build failed: {error}", file=sys.stderr)
        return 1
    finally:
        (output / "build-result.json").write_text(json.dumps(result, indent=2) + "\n")


def run(workspace: Path, output: Path) -> int:
    workspace, output = workspace.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "gate-result.json").unlink(missing_ok=True)
    result = {"kind": "rust", "status": "failed", "exit_code": 1}
    try:
        selected = suites(workspace)
        if not selected:
            result.update(status="legacy_skipped", exit_code=0,
                          reason="Desktop ref predates the PDF target")
            return 0
        result["desktop_sha"] = desktop_revision(workspace)
        reports = [run_suite(suite, workspace, output / suite.label) for suite in selected]
        code = next((report["exit_code"] for report in reports if report["status"] != "passed"), 0)
        result.update(suites=reports, runnable_count=sum(report.get("runnable_count", 0) for report in reports),
                      status="passed" if code == 0 else "failed", exit_code=code)
        return code
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
    parser.add_argument("--build-only", action="store_true",
                        help="Compile all contracted targets without executing or passing the gate")
    args = parser.parse_args()
    return (build if args.build_only else run)(args.workspace, args.output)


if __name__ == "__main__":
    sys.exit(main())
