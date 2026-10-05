#!/usr/bin/env python3
"""Run exact-source Backup qualification on the private repository's isolated VM.

Restore activation owns durable journals and temporary services. A caller timeout or
cancellation must leave the private run alive to complete verification and cleanup.
"""

import os
import re
import signal
import time

from request_image_build import api


REPOSITORY = "AppFlowy-IO/AppFlowy-Cloud-Premium"
WORKFLOW = "backup-release-tests.yml"
API_ROOT = f"repos/{REPOSITORY}/actions"


def find_run(request, title):
    runs = request("GET", f"{API_ROOT}/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100")
    matches = [run for run in runs["workflow_runs"] if run["display_title"] == title]
    if len(matches) > 1:
        raise RuntimeError("Multiple Backup runs have the same request ID; refusing to guess")
    return matches[0] if matches else None


def workflow_ref(source_ref, request):
    if not source_ref or re.search(r"[\x00-\x20\x7f]", source_ref):
        raise ValueError("Expected a Cloud branch, tag, pull-request ref, or commit")
    if re.fullmatch(r"[a-f0-9]{40}", source_ref):
        # Dispatch requires a branch or tag. The workflow still checks out the
        # immutable source_sha input, independently of its own definition.
        return "main"
    pull = re.fullmatch(r"refs/pull/([1-9][0-9]*)/(head|merge)", source_ref)
    if pull:
        data = request("GET", f"repos/{REPOSITORY}/pulls/{pull[1]}")
        if data["head"]["repo"]["full_name"] != REPOSITORY:
            raise ValueError("Backup workflow must come from the private Cloud repository")
        return data["head"]["ref"]
    for prefix in ("refs/heads/", "refs/tags/"):
        if source_ref.startswith(prefix):
            return source_ref[len(prefix):]
    if source_ref.startswith("refs/"):
        raise ValueError("Unsupported Cloud workflow ref")
    return source_ref


def request_tests(source_sha, request_id, source_ref, request=api, wait=time.sleep,
                  now=time.monotonic, timeout=345 * 60):
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("Expected an immutable 40-character Cloud commit")
    if not re.fullmatch(r"[0-9]+-[0-9]+", request_id):
        raise ValueError("Expected the AppFlowy-CI run ID and attempt")
    ref = workflow_ref(source_ref, request)
    inputs = {"suite": "ci", "source_sha": source_sha, "request_id": request_id}
    title = f"CI backup {request_id} ({source_sha})"
    started = now()
    deadline = started + timeout
    run = find_run(request, title)
    if run is None:
        # The API wrapper never retries writes: an ambiguous response may already
        # have started a real restore. A retry can find the correlated run instead.
        request("POST", f"{API_ROOT}/workflows/{WORKFLOW}/dispatches", {
            "ref": ref, "inputs": inputs,
        })
    discovery_deadline = min(deadline, now() + 180)
    while run is None and now() < discovery_deadline:
        run = find_run(request, title)
        if run is None:
            wait(5)
    if run is None:
        raise TimeoutError("GitHub did not expose the dispatched Backup run within 3 minutes; "
                           "inspect private Backup runs before retrying")

    run_id = int(run["id"])
    url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"run_id={run_id}\nrun_url={url}", file=output)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        print(f"Backup qualification for `{source_sha}`: [private run and evidence]({url}).\n\n"
              "This run continues cleanup if the public requester is cancelled.", file=summary)
    print(f"Waiting for {url}", flush=True)
    last_status = None
    last_report = started
    try:
        while True:
            current = now()
            if run["status"] != last_status or current - last_report >= 60:
                print(f"Private Backup run {run_id}: {run['status']} "
                      f"({int((current - started) / 60)}m elapsed)", flush=True)
                last_status = run["status"]
                last_report = current
            if run["status"] == "completed":
                break
            if current >= deadline:
                raise TimeoutError(f"Timed out waiting for Backup qualification: {url}")
            wait(15)
            run = request("GET", f"{API_ROOT}/runs/{run_id}")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"conclusion={run['conclusion']}", file=output)
        artifacts = request("GET", f"{API_ROOT}/runs/{run_id}/artifacts?per_page=100")
        expected = f"backup-integration-{request_id}"
        evidence = [item for item in artifacts["artifacts"]
                    if item["name"] == expected and not item["expired"]]
        if len(evidence) == 1:
            artifact_id = int(evidence[0]["id"])
            artifact_url = f"{url}/artifacts/{artifact_id}"
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                print(f"artifact_id={artifact_id}\nartifact_url={artifact_url}", file=output)
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                print(f"Backup qualification: **{run['conclusion']}**. "
                      f"[Download private evidence]({artifact_url}).", file=summary)
        if run["conclusion"] != "success":
            raise RuntimeError(f"Backup qualification {run['conclusion']}: {url}")
        if len(evidence) != 1:
            raise RuntimeError(f"Backup qualification needs exactly one evidence artifact {expected}: {url}")
        return run_id
    finally:
        if run["status"] != "completed":
            print(f"::warning::Backup verification and owned-resource cleanup continue at {url}. "
                  "Do not cancel the private run during restore activation.", flush=True)


def interrupted(_signum, _frame):
    raise InterruptedError("The public Backup requester was cancelled; private cleanup continues")


def main():
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    request_tests(
        os.environ["SOURCE_SHA"],
        f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
        os.environ["SOURCE_REF"],
    )


if __name__ == "__main__":
    main()
