#!/usr/bin/env python3
"""Publish a PDF gate status only for the exact private Desktop revision tested."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

from run_desktop_pdf_tests import CONTRACT, TEST_SOURCE


CONTEXTS = {"rust": "PDF export regression (Rust)", "ui": "PDF export regression (UI)",
            "cloud_ui": "PDF export regression (Cloud UI)"}
REPOSITORY = "AppFlowy-IO/AppFlowy-Premium"
PROTECTED = {f"frontend/rust-lib/{CONTRACT.as_posix()}",
             f"frontend/rust-lib/{TEST_SOURCE.as_posix()}",
             "frontend/appflowy_flutter/integration_test/desktop/bdd/database/grid/database_export_test.dart",
             "frontend/appflowy_flutter/integration_test/desktop/bdd/database/cloud_database_export/cloud_database_export_test.dart"}


def github_json(method: str, path: str, data: dict | None = None):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        data=json.dumps(data).encode() if data is not None else None, method=method,
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != (201 if method == "POST" else 200):
            raise ValueError(f"Unexpected GitHub API response: {response.status}")
        return json.load(response)


def removed_contract_files(pr_number: str) -> set[str]:
    removed = set()
    for page in range(1, 31):
        files = github_json("GET", f"pulls/{pr_number}/files?per_page=100&page={page}")
        if not isinstance(files, list):
            raise ValueError("Cannot verify the PDF contract's PR changes")
        for file in files:
            if file.get("status") == "removed" and file.get("filename") in PROTECTED:
                removed.add(file["filename"])
            if file.get("status") == "renamed" and file.get("previous_filename") in PROTECTED:
                removed.add(file["previous_filename"])
        if len(files) < 100:
            return removed
    raise ValueError("PR changed-file inventory exceeded GitHub's 3000-file limit")


def legacy_source(workspace: Path, kind: str) -> bool:
    if kind == "rust":
        feature = workspace / TEST_SOURCE
    else:
        entrypoint = ("cloud_database_export/cloud_database_export_test.dart" if kind == "cloud_ui"
                      else "grid/database_export_test.dart")
        feature = workspace.parent / "appflowy_flutter/integration_test/desktop/bdd/database" / entrypoint
    return not (workspace / CONTRACT).exists() and not feature.exists()


def source_sha(workspace: Path, event: dict, event_name: str, kind: str = "rust") -> str | None:
    if event_name not in {"repository_dispatch", "workflow_dispatch"}:
        return None
    payload = event.get("client_payload", {}) if event_name == "repository_dispatch" else event.get("inputs", {})
    # Only PR-triggered private runs publish cross-repository statuses.
    if not re.fullmatch(r"[1-9][0-9]*", str(payload.get("pr_number", ""))):
        return None
    expected = payload.get("pr_ref", "")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{40}", expected):
        raise ValueError("PDF status requires an immutable Desktop pr_ref SHA")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    if actual != expected:
        raise ValueError(f"Desktop checkout {actual} does not match requested SHA {expected}")
    return actual


def outcome(workspace: Path, kind: str, state: str, result: dict,
            sha: str, job_status: str, gate_outcome: str = "success") -> tuple[str, str]:
    if legacy_source(workspace, kind):
        return "success", "Not applicable: legacy Desktop ref has no PDF contract/test"
    if state == "completed":
        # Missing result (including a failed prerequisite) must never become success.
        passed = (job_status == "success" and gate_outcome == "success" and result.get("status") == "passed"
                  and result.get("exit_code") == 0 and result.get("kind") == kind
                  and result.get("desktop_sha") == sha and result.get("runnable_count", 0) > 0)
        state = "success" if passed else "failure"
    return state, {"pending": "Running PDF inventory and saved-content checks",
                   "success": "All required PDF saved-content checks passed",
                   "failure": "PDF checks or their prerequisites failed"}[state]


def publish(workspace: Path, state: str, gate_result: Path | None, kind: str = "rust",
            job_status: str = "success", gate_outcome: str = "skipped") -> int:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    sha = source_sha(workspace, event, os.environ["GITHUB_EVENT_NAME"], kind)
    if sha is None:
        print("No private Desktop PR revision to report")
        return 0
    payload = event.get("client_payload", {}) if os.environ["GITHUB_EVENT_NAME"] == "repository_dispatch" else event.get("inputs", {})
    pr_number = str(payload["pr_number"])
    pr = github_json("GET", f"pulls/{pr_number}")
    if pr.get("head", {}).get("sha") != sha:
        print("Desktop PR advanced; skipping this older revision's status")
        return 0
    if state == "pending" and gate_result:
        gate_result.unlink(missing_ok=True)
    result = json.loads(gate_result.read_text()) if gate_result and gate_result.exists() else {}
    state, description = outcome(workspace, kind, state, result, sha, job_status, gate_outcome)
    # A legacy head can legitimately lack the contract. A PR that removed it
    # cannot use that backward-compatibility path to weaken the required gate.
    # Contracted migrations remain governed by the new entrypoint/test inventory.
    removed = removed_contract_files(pr_number) if not (workspace / CONTRACT).exists() else set()
    if removed:
        state, description = "failure", "PDF contract/test removed or renamed; preserve explicit coverage"
    payload = {"state": state, "context": CONTEXTS[kind], "description": description,
               "target_url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"}
    github_json("POST", f"statuses/{sha}", payload)
    print(f"Published {CONTEXTS[kind]}: {state} for {sha}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--state", required=True, choices=["pending", "completed"])
    parser.add_argument("--gate-result", type=Path)
    parser.add_argument("--kind", choices=CONTEXTS, default="rust")
    parser.add_argument("--job-status", default="success")
    parser.add_argument("--gate-outcome", default="skipped")
    args = parser.parse_args()
    try:
        return publish(args.workspace, args.state, args.gate_result, args.kind, args.job_status, args.gate_outcome)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"PDF status reporting failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
