"""Locate the source-matched dependency manifest without reading it into public job outputs."""

import os
import re
import time

from request_image_build import API_ROOT, REPOSITORY, WORKFLOW, api


def locate(source, parent, attempt, pr, *, request=api, now=time.monotonic, sleep=time.sleep):
    if (not re.fullmatch(r"[a-f0-9]{40}", source) or not parent.isdecimal() or attempt < 1
            or (pr and not pr.isdecimal())):
        raise ValueError("Invalid private build correlation")
    if request("GET", f"repos/{REPOSITORY}").get("private") is not True:
        raise RuntimeError("Dependency manifests require a private producer repository")
    suffix = f" [PR #{pr}]" if pr else ""
    title = re.compile(rf"CI images {parent}-([1-9][0-9]*) \({source}\){re.escape(suffix)}")
    deadline = now() + 1200
    while now() < deadline:
        runs = request("GET", f"{API_ROOT}/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100")
        for run in runs["workflow_runs"]:
            match = title.fullmatch(run["display_title"])
            if not match or int(match[1]) > attempt:
                continue
            name = f"private-image-lock-{parent}-{match[1]}"
            run_id = int(run["id"])
            items = request("GET", f"{API_ROOT}/runs/{run_id}/artifacts?per_page=100")["artifacts"]
            if any(a["name"] == name and not a["expired"] and a["workflow_run"]["id"] == run_id
                   for a in items):
                return run_id, name
            if int(match[1]) == attempt and run["status"] == "completed":
                raise RuntimeError("The private image builder completed without its dependency manifest")
        sleep(10)
    raise TimeoutError("The private image builder did not publish its dependency manifest in time")


if __name__ == "__main__":
    run_id, name = locate(os.environ["SOURCE_SHA"], os.environ["GITHUB_RUN_ID"],
                          int(os.environ["GITHUB_RUN_ATTEMPT"]), os.environ.get("PR_NUMBER", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print(f"run_id={run_id}", file=output)
        print(f"artifact={name}", file=output)
