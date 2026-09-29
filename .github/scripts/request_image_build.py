#!/usr/bin/env python3
"""Build one pinned Cloud revision in the private runner repository."""

import json
import os
import re
import signal
import subprocess
import time


REPOSITORY = "AppFlowy-IO/AppFlowy-Cloud-Premium"
WORKFLOW = "build_ci_images_self_hosted.yml"
API_ROOT = f"repos/{REPOSITORY}/actions"
ARTIFACTS = {f"docker-image-{service}" for service in ("cloud", "worker", "search", "mcp")}


def api(method, path, payload=None):
    command = ["gh", "api", "--method", method, path]
    if payload is not None:
        command += ["--input", "-"]
    # Only reads are retried. Repeating an ambiguous dispatch can create two builds.
    for attempt in range(3 if method == "GET" else 1):
        try:
            result = subprocess.run(
                command, input=json.dumps(payload) if payload is not None else None,
                text=True, capture_output=True, check=True, timeout=30,
            )
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            if method != "GET" or attempt == 2:
                raise RuntimeError(
                    f"GitHub API {method} {path} failed. Confirm the Premium workflow "
                    "is merged and ADMIN_GITHUB_TOKEN has access with Actions write permission."
                ) from error
            time.sleep(2)


def find_run(request, title):
    # Correlation includes the caller's run attempt and source SHA. Never select
    # the most recent run: another CI request may dispatch at the same time.
    runs = request("GET", f"{API_ROOT}/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100")
    matches = [run for run in runs["workflow_runs"] if run["display_title"] == title]
    if len(matches) > 1:
        raise RuntimeError("Multiple image builds have the same request ID; refusing to guess")
    return matches[0] if matches else None


def request_build(source_sha, request_id, request=api, wait=time.sleep,
                  now=time.monotonic, timeout=330 * 60):
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("Expected an immutable 40-character Cloud commit")
    if not re.fullmatch(r"[0-9]+-[0-9]+", request_id):
        raise ValueError("Expected the AppFlowy-CI run ID and attempt")
    title = f"CI images {request_id} ({source_sha})"
    started = now()
    deadline = started + timeout
    run = find_run(request, title)
    submitted = run is not None
    try:
        if run is None:
            submitted = True
            request("POST", f"{API_ROOT}/workflows/{WORKFLOW}/dispatches", {
                "ref": "main", "inputs": {"source_sha": source_sha, "request_id": request_id},
            })
        discovery_deadline = now() + 180
        while run is None and now() < discovery_deadline:
            run = find_run(request, title)
            if run is None:
                wait(5)
        if run is None:
            raise TimeoutError("GitHub did not expose the dispatched image build within 3 minutes")

        run_id = int(run["id"])
        url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
        print(f"Waiting for {url}", flush=True)
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            print(f"Images for {source_sha}: [self-hosted build]({url}).", file=summary)
        last_status = None
        last_report = started
        while True:
            current = now()
            if run["status"] != last_status or current - last_report >= 60:
                print(f"Private image build {run_id}: {run['status']} "
                      f"({int((current - started) / 60)}m elapsed)", flush=True)
                last_status = run["status"]
                last_report = current
            if run["status"] == "completed":
                break
            if current >= deadline:
                raise TimeoutError(f"Timed out waiting for {url}")
            wait(15)
            run = request("GET", f"{API_ROOT}/runs/{run_id}")
        if run["conclusion"] != "success":
            raise RuntimeError(f"Image build {run['conclusion']}: {url}")
        artifacts = request("GET", f"{API_ROOT}/runs/{run_id}/artifacts?per_page=100")
        available = {item["name"] for item in artifacts["artifacts"] if not item["expired"]}
        if not ARTIFACTS.issubset(available):
            raise RuntimeError(f"Image build is missing artifacts: {sorted(ARTIFACTS - available)}")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"run_id={run_id}", file=output)
        return run_id
    finally:
        # Best-effort cancellation of this request only, including an ambiguous
        # dispatch response. Never cancel a release or another caller's build.
        if submitted and (run is None or run["status"] != "completed"):
            try:
                run = run or find_run(request, title)
                if run is not None and run["status"] != "completed":
                    request("POST", f"{API_ROOT}/runs/{int(run['id'])}/cancel")
            except Exception as error:
                print(f"::warning::Could not cancel the private image build: {error}", flush=True)


def interrupted(_signum, _frame):
    raise InterruptedError("The requesting CI job was cancelled")


def main():
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    request_build(
        os.environ["SOURCE_SHA"],
        f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
    )


if __name__ == "__main__":
    main()
