#!/usr/bin/env python3
"""Cancel CI runs whose source PR closed or whose exact parent attempt was cancelled."""

import argparse
from functools import cache
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode


CI_REPO = "AppFlowy-IO/AppFlowy-CI"
CLOUD_REPO = "AppFlowy-IO/AppFlowy-Cloud-Premium"
CLIENT_REPO = "AppFlowy-IO/AppFlowy-Premium"
SOURCES = {CI_REPO, CLOUD_REPO, CLIENT_REPO}
ACTIVE = ("queued", "in_progress", "pending", "waiting", "requested")
CANCELLED = {"cancelled", "timed_out"}
# Limit cleanup to CI workflows, never Docker release workflows.
WORKFLOWS = {
    "webhook_receiver.yaml": SOURCES - {CI_REPO},
    "workflow_checks.yaml": {CI_REPO},
    **{name: {CI_REPO, CLIENT_REPO} for name in (
        "flutter_ci.yaml", "ios_ci.yaml", "rust_ci.yaml",
    )},
    **{name: {CLIENT_REPO} for name in ("mobile_ci.yml", "rust_coverage.yml")},
    **{name: {CLOUD_REPO} for name in (
        "cloud_backend_ci.yaml", "cloud_commercial_integration_ci.yaml",
        "cloud_docker_ci.yaml", "cloud_e2e_ci.yaml", "cloud_frontend_ci.yaml",
        "cloud_integration_ci.yaml", "cloud_rustlint_ci.yaml",
    )},
}
IDENTITY = re.compile(
    r"^\[ci source=(AppFlowy-IO/[A-Za-z-]+) pr=(none|[1-9][0-9]*) "
    r"parent=(none|[1-9][0-9]*-[1-9][0-9]*)\] "
)
IMAGE_REQUEST = re.compile(r"CI images ([1-9][0-9]*)-([1-9][0-9]*) \([a-f0-9]{40}\)")
CHILD_WORKFLOWS = {
    "flutter": "flutter_ci.yaml", "rust": "rust_ci.yaml", "mobile": "mobile_ci.yml",
    "ios": "ios_ci.yaml", "rust_coverage": "rust_coverage.yml",
    "docker": "docker_ci.yml", "commit_lint": "commit_lint.yml",
    "backend": "cloud_backend_ci.yaml", "frontend": "cloud_frontend_ci.yaml",
    "e2e": "cloud_e2e_ci.yaml", "cloud_docker": "cloud_docker_ci.yaml",
    "rustlint": "cloud_rustlint_ci.yaml", "integration": "cloud_integration_ci.yaml",
    "commercial": "cloud_commercial_integration_ci.yaml",
}


class ApiError(RuntimeError):
    pass


def api(method, path):
    result = subprocess.run(
        ["gh", "api", "--method", method, path], text=True,
        capture_output=True, timeout=30,
    )
    if result.returncode:
        raise ApiError(f"GitHub API {method} {path} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def identity(run):
    workflow = Path(run["path"]).name
    match = IDENTITY.match(run.get("display_title", ""))
    if not match or match[1] not in WORKFLOWS.get(workflow, set()):
        return None
    source, pr, parent = match.groups()
    if run["event"] == "pull_request" and source != CI_REPO:
        return None
    if run["event"] not in {"pull_request", "repository_dispatch", "workflow_dispatch"}:
        return None
    return source, int(pr) if pr != "none" else None, (
        tuple(map(int, parent.split("-"))) if parent != "none" else None
    )


def active_runs(repo, request):
    seen = set()
    for status in ACTIVE:
        page = 1
        while True:
            runs = request(
                "GET", f"repos/{repo}/actions/runs?status={status}&per_page=100&page={page}",
            )["workflow_runs"]
            for run in runs:
                if run["status"] != "completed" and run["id"] not in seen:
                    seen.add(run["id"])
                    yield run
            if len(runs) < 100:
                break
            page += 1


def pr_is_open(source, number, request=api):
    if source not in SOURCES or not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise ValueError("Expected an allowed source repository and positive PR number")
    return request("GET", f"repos/{source}/pulls/{number}")["state"] == "open"


def find_children(names, parent, request=api, wait=time.sleep, now=time.monotonic):
    """Find children by the dispatcher's run ID and attempt, never their start order."""
    expected = {CHILD_WORKFLOWS[name]: name for name in names}
    started = request(
        "GET", f"repos/{CI_REPO}/actions/runs/{parent[0]}/attempts/{parent[1]}",
    )["run_started_at"]
    deadline = now() + 180
    while True:
        found = {}
        page = 1
        while True:
            query = urlencode({
                "event": "repository_dispatch", "created": f">={started}",
                "per_page": 100, "page": page,
            })
            runs = request("GET", f"repos/{CI_REPO}/actions/runs?{query}")["workflow_runs"]
            for run in runs:
                name = expected.get(Path(run["path"]).name)
                info = identity(run)
                if name and info and info[2] == parent:
                    if name in found and found[name] != run["id"]:
                        raise RuntimeError(f"Multiple {name} runs for parent attempt {parent}")
                    found[name] = run["id"]
            if len(runs) < 100:
                break
            page += 1
        if len(found) == len(expected):
            return found
        if now() >= deadline:
            raise TimeoutError(f"Child workflows not found: {sorted(set(names) - found.keys())}")
        wait(5)


def cleanup(request=api, *, apply=False):
    """Read trusted API identities; missing legacy dispatch metadata is never guessed."""
    cancelled = set()
    planned = []
    errors = []

    @cache
    def closed(source, number):
        return not pr_is_open(source, number, request)

    @cache
    def parent_cancelled(run_id, attempt):
        parent = request(
            "GET", f"repos/{CI_REPO}/actions/runs/{run_id}/attempts/{attempt}",
        )
        return parent["status"] == "completed" and parent["conclusion"] in CANCELLED

    def cancel(repo, run, reason):
        if apply:
            current = request("GET", f"repos/{repo}/actions/runs/{run['id']}")
            # The cancellation endpoint targets the current attempt, so recheck
            # immediately before writing if the run was retried during this scan.
            if current["status"] == "completed" or current["run_attempt"] != run["run_attempt"]:
                return
        print(f"{'Cancel' if apply else 'Would cancel'} {repo} run {run['id']}: {reason}", flush=True)
        if apply:
            try:
                request("POST", f"repos/{repo}/actions/runs/{run['id']}/cancel")
            except ApiError:
                # Finishing between the list and cancellation is harmless. Other
                # failures must stay visible, and must not stop unrelated cleanup.
                current = request("GET", f"repos/{repo}/actions/runs/{run['id']}")
                if current["status"] != "completed":
                    raise
                return
        planned.append((repo, run["id"]))
        if repo == CI_REPO:
            cancelled.add((run["id"], run["run_attempt"]))

    runs = list(active_runs(CI_REPO, request))
    for run in runs:
        try:
            info = identity(run)
            if info and info[1] and closed(info[0], info[1]):
                cancel(CI_REPO, run, f"{info[0]} PR #{info[1]} is closed")
        except (ApiError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))

    for run in runs:
        if (run["id"], run["run_attempt"]) in cancelled:
            continue
        try:
            info = identity(run)
            if info and info[2] and (
                info[2] in cancelled or parent_cancelled(*info[2])
            ):
                cancel(CI_REPO, run, f"parent attempt {info[2][0]}-{info[2][1]} was cancelled")
        except (ApiError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))

    for run in list(active_runs(CLOUD_REPO, request)):
        if Path(run["path"]).name != "build_ci_images_self_hosted.yml":
            continue
        match = IMAGE_REQUEST.fullmatch(run.get("display_title", ""))
        if run["event"] != "workflow_dispatch" or not match:
            continue
        parent = tuple(map(int, match.groups()))
        try:
            if parent in cancelled or parent_cancelled(*parent):
                cancel(CLOUD_REPO, run, f"requesting CI attempt {parent[0]}-{parent[1]} was cancelled")
        except (ApiError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError("\n".join(errors))
    print(f"{len(planned)} obsolete runs {'cancelled' if apply else 'identified'}.", flush=True)
    return planned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Cancel runs; default is a dry run")
    parser.add_argument("--check-pr", action="store_true", help="Gate new dispatches on PR state")
    parser.add_argument("--find-children", action="store_true", help="Find this dispatcher's children")
    args = parser.parse_args()
    if args.check_pr:
        number = os.environ.get("PR_NUMBER", "")
        active = not number or pr_is_open(os.environ["SOURCE_REPOSITORY"], number)
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            print(f"active={str(active).lower()}", file=output)
        if not active:
            print(f"PR #{number} is closed; no child workflows will be dispatched.")
    elif args.find_children:
        found = find_children(
            os.environ["WORKFLOWS"].split(","),
            (int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])),
        )
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for name, run_id in found.items():
                url = f"https://github.com/{CI_REPO}/actions/runs/{run_id}"
                print(f"{name}_run_url={url}", file=output)
                print(f"{name}: {url}")
    else:
        cleanup(apply=args.apply)


if __name__ == "__main__":
    main()
