#!/usr/bin/env python3
"""Run the source-contracted desktop PDF BDD and verify machine test events."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

from run_desktop_pdf_tests import CONTRACT, desktop_revision


LEGACY_ENTRYPOINT = "integration_test/desktop/bdd/database/grid/database_export_test.dart"
CLOUD_ENTRYPOINT = "integration_test/desktop/bdd/database/cloud_database_export/cloud_database_export_test.dart"


def configuration(project: Path, kind: str) -> tuple[Path, list[str], bool]:
    contract = project.parent / "rust-lib" / CONTRACT
    required = []
    entrypoint = CLOUD_ENTRYPOINT if kind == "cloud_ui" else LEGACY_ENTRYPOINT
    if contract.exists():
        data = json.loads(contract.read_text()).get(kind, {})
        entrypoint, required = data.get("entrypoint"), data.get("required_tests")
        if not isinstance(entrypoint, str) or not isinstance(required, list) or not required or any(
            not isinstance(name, str) or not name.strip() for name in required
        ):
            raise ValueError(f"Desktop PDF contract must name its {kind} entrypoint and required tests")
    path = (project / entrypoint).resolve()
    if not path.is_relative_to(project) or not entrypoint.endswith("_test.dart"):
        raise ValueError("PDF UI entrypoint must be a test inside the Flutter project")
    if not path.exists() and contract.exists():
        raise ValueError("Desktop PDF contract requires a missing UI entrypoint")
    return path, required, not path.exists()


def completed_tests(events: Path) -> set[str]:
    names = {}
    completed = set()
    finished = False
    for line in events.read_text().splitlines():
        event = json.loads(line)
        if event.get("type") == "testStart":
            test = event["test"]
            names[test["id"]] = test["name"]
        elif event.get("type") == "testDone":
            if event.get("result") == "success" and not event.get("skipped", False) and not event.get("hidden", False):
                completed.add(names.get(event["testID"], ""))
        elif event.get("type") == "error":
            raise ValueError("Flutter reported an error while running the PDF BDD")
        elif event.get("type") == "done":
            finished = event.get("success") is True
    if not finished:
        raise ValueError("PDF BDD did not finish with a successful machine test result")
    return completed - {""}


def verify_cloud(project: Path, output: Path, events: Path, process_status: int) -> int:
    project, output = project.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "gate-result.json").unlink(missing_ok=True)
    if events.resolve() != (output / "test-events.jsonl").resolve():
        (output / "test-events.jsonl").unlink(missing_ok=True)
    result = {"kind": "cloud_ui", "status": "failed", "exit_code": process_status}
    try:
        _, required, legacy = configuration(project, "cloud_ui")
        if legacy:
            result["status"] = "legacy_skipped"
            return 0
        result["desktop_sha"] = desktop_revision(project)
        if events.exists() and events.resolve() != (output / "test-events.jsonl").resolve():
            shutil.copyfile(events, output / "test-events.jsonl")
        if process_status:
            return process_status
        completed = completed_tests(events)
        missing = set(required) - completed
        if not completed or missing:
            raise ValueError(f"Cloud PDF BDD must execute its required non-skipped tests: missing={sorted(missing)}")
        result.update(status="passed", runnable_count=len(completed), completed_tests=sorted(completed))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        print(f"Desktop cloud PDF UI gate failed: {error}", file=sys.stderr)
        return 1
    finally:
        (output / "gate-result.json").write_text(json.dumps(result, indent=2) + "\n")


def run(project: Path, output: Path, device: str = "macos") -> int:
    project, output = project.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "gate-result.json").unlink(missing_ok=True)
    (output / "test-events.jsonl").unlink(missing_ok=True)
    result = {"kind": "ui", "status": "failed", "device": device}
    try:
        path, required, legacy = configuration(project, "ui")
        if legacy:
            result.update(status="legacy_skipped", reason="Desktop ref predates the PDF BDD")
            return 0
        result["desktop_sha"] = desktop_revision(project)
        events = output / "test-events.jsonl"
        command = ["flutter", "test", str(path.relative_to(project)), "-d", device,
                   f"--file-reporter=json:{events}",
                   "--dart-define=APPFLOWY_TEST_BACKGROUND_FRAMES=true"]
        result["command"] = command
        environment = {**os.environ, "APPFLOWY_PDF_TEST_OUTPUT_DIR": str(output / "saved-exports")}
        (output / "saved-exports").mkdir(exist_ok=True)
        with (output / "test.log").open("w") as log:
            process = subprocess.run(command, cwd=project, env=environment, stdout=log,
                                     stderr=subprocess.STDOUT, check=False)
        result["exit_code"] = process.returncode
        if process.returncode:
            return process.returncode
        completed = completed_tests(events)
        missing = set(required) - completed
        if not completed or missing:
            raise ValueError(f"PDF BDD must execute its required non-skipped tests: missing={sorted(missing)}")
        result.update(status="passed", runnable_count=len(completed), completed_tests=sorted(completed))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        print(f"Desktop PDF UI gate failed: {error}", file=sys.stderr)
        return 1
    finally:
        (output / "gate-result.json").write_text(json.dumps(result, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--device", choices=["macos", "linux"], default="macos")
    parser.add_argument("--verify-cloud-events", type=Path)
    parser.add_argument("--process-status", type=int, default=0)
    args = parser.parse_args()
    if args.verify_cloud_events:
        return verify_cloud(args.project, args.output, args.verify_cloud_events, args.process_status)
    return run(args.project, args.output, args.device)


if __name__ == "__main__":
    sys.exit(main())
