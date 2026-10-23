#!/usr/bin/env python3
"""Cancel merged-PR CI runs and obsolete Cloud integration image builds."""

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
CLIENT_REPO = "AppFlowy-IO/AppFlowy-Premium"
CLOUD_REPO = "AppFlowy-IO/AppFlowy-Cloud-Premium"
ACTIVE = ("queued", "in_progress", "pending", "waiting", "requested")
CANCELLED = {"cancelled", "timed_out"}
# This is the only public workflow that requests laptop image builds.
INTEGRATION_WORKFLOW = "cloud_integration_ci.yaml"
SOURCE_WORKFLOWS = {
    CLIENT_REPO: {
        "flutter_ci.yaml", "rust_ci.yaml", "ios_ci.yaml", "mobile_ci.yml",
        "rust_coverage.yml", "webhook_receiver.yaml",
    },
    CLOUD_REPO: {
        "cloud_backend_ci.yaml", "cloud_frontend_ci.yaml", "cloud_e2e_ci.yaml",
        "cloud_docker_ci.yaml", "cloud_rustlint_ci.yaml", INTEGRATION_WORKFLOW,
        "cloud_commercial_integration_ci.yaml", "webhook_receiver.yaml",
    },
}
IDENTITY = re.compile(
    r"^\[ci(?:-v2)? source=(AppFlowy-IO/[A-Za-z-]+) pr=(none|[1-9][0-9]*) "
    r"parent=(none|[1-9][0-9]*-[1-9][0-9]*)\] "
)
# The PR suffix is display-only. Keep it optional so cleanup can still identify
# image runs dispatched by older AppFlowy-CI revisions.
IMAGE_REQUEST = re.compile(
    r"CI images ([1-9][0-9]*)-([1-9][0-9]*) \([a-f0-9]{40}\)"
    r"(?: \[PR #[1-9][0-9]*\])?"
)
# #42 dispatchers check out default-branch scripts, including on retries. Retain
# their read-only lookup interface without enrolling these workflows in cleanup.
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
    if not match or run["event"] not in {"repository_dispatch", "workflow_dispatch"}:
        return None
    source, pr, parent = match.groups()
    if run["display_title"].startswith("[ci-v2 "):
        if workflow not in SOURCE_WORKFLOWS.get(source, set()):
            return None
    else:
        if source != CLOUD_REPO or workflow != INTEGRATION_WORKFLOW:
            return None
        # Old dispatch titles included PR numbers for push events too. They can
        # identify image requesters, but cannot authorize merged-PR cleanup.
        pr = "none"
    return source, int(pr) if pr != "none" else None, (
        tuple(map(int, parent.split("-"))) if parent != "none" else None
    )


def native_pr_number(run):
    """Native PR ownership comes from GitHub's association, never the title."""
    associations = run.get("pull_requests")
    if run.get("event") != "pull_request" or not isinstance(associations, list):
        return None
    # An ambiguous association must not cancel a run still serving another PR.
    if len(associations) != 1 or not isinstance(associations[0], dict):
        return None
    association = associations[0]
    number = association.get("number")
    base = association.get("base")
    if type(number) is not int or number <= 0 or not isinstance(base, dict):
        return None
    repo = base.get("repo")
    repo_url = f"https://api.github.com/repos/{CI_REPO}"
    if not isinstance(repo, dict) or repo.get("url") != repo_url:
        return None
    if association.get("url") != f"{repo_url}/pulls/{number}":
        return None
    return number


def pr_is_merged(source, number, request=api):
    if source not in {CI_REPO, *SOURCE_WORKFLOWS} or type(number) is not int or number <= 0:
        raise ValueError("Expected a supported source repository and a positive PR number")
    pr = request("GET", f"repos/{source}/pulls/{number}")
    merged_at = pr.get("merged_at")
    return pr.get("state") == "closed" and (
        pr.get("merged") is True or isinstance(merged_at, str) and bool(merged_at)
    )


def cloud_integration(run):
    info = identity(run)
    if info and info[0] == CLOUD_REPO and Path(run["path"]).name == INTEGRATION_WORKFLOW:
        return info
    return None


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


def find_children(names, parent, request=api, wait=time.sleep, now=time.monotonic):
    """Serve old dispatchers; only integration still requires parent metadata."""
    expected = {CHILD_WORKFLOWS[name]: name for name in names}
    started = request(
        "GET", f"repos/{CI_REPO}/actions/runs/{parent[0]}/attempts/{parent[1]}",
    )["run_started_at"]
    deadline = now() + 180
    while True:
        found = {}
        restored = {}
        page = 1
        while True:
            query = urlencode({
                "event": "repository_dispatch", "created": f">={started}",
                "per_page": 100, "page": page,
            })
            runs = request("GET", f"repos/{CI_REPO}/actions/runs?{query}")["workflow_runs"]
            for run in runs:
                workflow = Path(run["path"]).name
                name = expected.get(workflow)
                if not name:
                    continue
                match = IDENTITY.match(run.get("display_title", ""))
                if match and match[3] == f"{parent[0]}-{parent[1]}":
                    if name in found and found[name] != run["id"]:
                        raise RuntimeError(f"Multiple {name} runs for parent attempt {parent}")
                    found[name] = run["id"]
                elif workflow != INTEGRATION_WORKFLOW and (not match or match[3] == "none"):
                    # Non-integration workflows no longer forward a parent.
                    # Preserve old callers' newest-run lookup with v2 titles too.
                    restored.setdefault(name, run["id"])
            if len(runs) < 100:
                break
            page += 1
        found = restored | found
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
    def merged(source, number):
        return pr_is_merged(source, number, request)

    @cache
    def parent_run(run_id, attempt):
        return request(
            "GET", f"repos/{CI_REPO}/actions/runs/{run_id}/attempts/{attempt}",
        )

    def parent_cancelled(parent):
        return parent["status"] == "completed" and parent["conclusion"] in CANCELLED

    def cancel(repo, run, reason, *, native_pr=None):
        if apply:
            current = request("GET", f"repos/{repo}/actions/runs/{run['id']}")
            # The cancellation endpoint targets the current attempt, so recheck
            # immediately before writing if the run was retried during this scan.
            if current["status"] == "completed" or current["run_attempt"] != run["run_attempt"]:
                return
            if native_pr is not None and native_pr_number(current) != native_pr:
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
        # Native and other source workflows cannot authorize private image or
        # integration-child cancellations by appearing in the lineage set.
        if repo == CI_REPO and cloud_integration(run):
            cancelled.add((run["id"], run["run_attempt"]))

    runs = list(active_runs(CI_REPO, request))
    for run in runs:
        try:
            native_pr = native_pr_number(run)
            if native_pr is not None and merged(CI_REPO, native_pr):
                cancel(CI_REPO, run, f"{CI_REPO} PR #{native_pr} is merged", native_pr=native_pr)
                continue
            info = identity(run)
            if info and info[1] and merged(info[0], info[1]):
                cancel(CI_REPO, run, f"{info[0]} PR #{info[1]} is merged")
        except (ApiError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))

    for run in runs:
        if (run["id"], run["run_attempt"]) in cancelled:
            continue
        try:
            info = cloud_integration(run)
            if info and info[2] and (
                info[2] in cancelled or parent_cancelled(parent_run(*info[2]))
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
            # Also scope already-finished requesters by workflow. A cancelled
            # client run must never authorize a private image cancellation.
            if parent not in cancelled:
                requester = parent_run(*parent)
                if (
                    Path(requester["path"]).name != INTEGRATION_WORKFLOW
                    or requester.get("event") not in {"repository_dispatch", "workflow_dispatch"}
                    or not parent_cancelled(requester)
                ):
                    continue
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
    parser.add_argument("--check-pr", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--find-children", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_pr:
        # The restored webhook has no PR gate. Keep queued #42 dispatchers
        # working without looking up unrelated repositories' PRs.
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            print("active=true", file=output)
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
