#!/usr/bin/env python3
"""Wait for dispatched CI runs and distinguish pending results from failures."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time


CI_REPO = "AppFlowy-IO/AppFlowy-CI"
WORKFLOW_NAMES = {
    "flutter": "Flutter CI/CD", "rust": "Rust Testing", "mobile": "Mobile CI",
    "ios": "iOS CI", "rust_coverage": "Rust Coverage", "docker": "Docker CI",
    "commit_lint": "Commit Lint", "backend": "Cloud Backend",
    "frontend": "Cloud Frontend", "e2e": "Cloud E2E", "cloud_docker": "Cloud Docker",
    "rustlint": "Cloud Rust Lint", "integration": "Cloud Integration",
    "commercial": "Commercial Integration",
}
RUN_URL = re.compile(r"https://github\.com/AppFlowy-IO/AppFlowy-CI/actions/runs/([1-9][0-9]*)/?")
ACTIVE = {"queued", "in_progress", "pending", "waiting", "requested"}
FAILURES = {"failure", "timed_out", "action_required", "startup_failure"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s+")


class ApiError(RuntimeError):
    pass


def api(path, *, raw=False):
    command = ["gh", "api", path]
    if raw:
        command.append("--allow-escape-sequences")
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise ApiError(f"GitHub API request failed: {result.stderr.strip()[:200]}")
        return result.stdout if raw else json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ApiError(f"GitHub API response unavailable: {error}") from error


def requested_workflows(environ):
    results = []
    for key in dict.fromkeys(value.strip() for value in environ.get("WORKFLOWS", "").split(",")):
        if not key:
            continue
        url = environ.get(f"{key.upper()}_RUN_URL", "")
        match = RUN_URL.fullmatch(url)
        result = {"key": key, "name": WORKFLOW_NAMES.get(key, key), "url": url}
        if key not in WORKFLOW_NAMES:
            result["error"] = "Unknown requested workflow."
        elif not match:
            result["error"] = "No valid dispatched run URL was provided."
        else:
            result["run_id"] = int(match[1])
        results.append(result)
    return results


def observe(workflows, request=api):
    results = []
    for workflow in workflows:
        result = dict(workflow)
        if "run_id" in result:
            try:
                run = request(f"repos/{CI_REPO}/actions/runs/{result['run_id']}")
                if not isinstance(run, dict) or run.get("id") != result["run_id"]:
                    raise ApiError("Run details did not identify the requested run.")
                result.update(status=run.get("status"), conclusion=run.get("conclusion"))
            except ApiError as error:
                result["error"] = str(error)
        results.append(result)
    return results


def passed(result):
    return result.get("status") == "completed" and result.get("conclusion") == "success"


def all_passed(results):
    # An empty request or missing result must never authorize a review.
    return bool(results) and all(passed(result) for result in results)


def wait_for_results(workflows, *, max_wait=7200, interval=30,
                     request=api, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + max_wait
    while True:
        results = observe(workflows, request)
        for result in results:
            print(f"{result['key']}: {result.get('status', 'unverified')} "
                  f"({result.get('conclusion') or 'no conclusion'})", flush=True)
        if not results or all(result.get("status") == "completed" for result in results):
            return results, False
        remaining = deadline - clock()
        if remaining <= 0:
            return results, True
        sleep(min(interval, remaining))


def inspect_jobs(results, request=api):
    for result in results:
        if passed(result) or "run_id" not in result:
            continue
        result["jobs"] = []
        try:
            page = 1
            while True:
                response = request(f"repos/{CI_REPO}/actions/runs/{result['run_id']}/jobs"
                                   f"?filter=latest&per_page=100&page={page}")
                jobs = response.get("jobs") if isinstance(response, dict) else None
                if not isinstance(jobs, list):
                    raise ApiError("Job details were not available.")
                result["jobs"].extend(jobs)
                if len(jobs) < 100:
                    break
                page += 1
        except ApiError as error:
            result["job_error"] = str(error)


def category(result):
    if result.get("conclusion") in FAILURES or any(
        job.get("conclusion") in FAILURES for job in result.get("jobs", [])
    ):
        return "failed"
    if result.get("status") in ACTIVE:
        return "pending"
    if result.get("status") == "completed" and result.get("conclusion"):
        return "passed" if passed(result) else "nonpassing"
    return "unverified"


def failure_excerpt(log, workflow):
    lines = [TIMESTAMP.sub("", ANSI.sub("", line)).strip() for line in log.splitlines()]
    patterns = {
        "rust": r"^(?:test .* \.\.\. FAILED|---- .* ----)$",
        "flutter": r"(?:^✖|^FAILED:.*test|^Error:|test.*FAILED)",
    }
    pattern = patterns.get(workflow)
    failures = [line for line in lines if pattern and re.search(pattern, line)]
    if not failures:
        failures = [line for line in lines if re.search(r"error:|failed|fatal:|panic", line, re.I)
                    and "0 failed" not in line]
    return list(dict.fromkeys(line[:200] for line in failures))[:20]


def failed_job_lines(job, workflow, request):
    lines = [f"- **Job**: {job.get('name', job['id'])} "
             f"({job.get('runner_name') or 'unassigned'}) — "
             f"[{job.get('conclusion')}]({job['html_url']})"]
    for step in job.get("steps", []):
        if step.get("conclusion") in FAILURES:
            lines.append(f"  - Failed step: {step['name']}")
    try:
        log = request(f"repos/{CI_REPO}/actions/jobs/{job['id']}/logs", raw=True)
        lines.extend(f"  - `{line.replace('`', chr(39))}`" for line in failure_excerpt(log, workflow))
    except ApiError:
        lines.append("  - Job logs are not available; follow the job link for details.")
    return lines


def render_report(results, pr_number, expired, request=api):
    groups = {name: [] for name in ("failed", "nonpassing", "pending", "unverified")}
    for result in results:
        kind = category(result)
        if kind != "passed":
            groups[kind].append(result)
    if groups["failed"]:
        title = "❌ **CI failures detected**"
    elif groups["nonpassing"]:
        title = "⚠️ **Some CI workflows did not pass**"
    elif groups["pending"]:
        title = "⏳ **CI is still pending**"
    elif not all_passed(results):
        title = "⚠️ **CI results could not be verified**"
    else:
        return f"✅ **All requested CI workflows passed** for PR {pr_number}.\n"
    lines = [f"{title} for PR {pr_number}.", ""]
    if expired:
        lines.extend(["The receiver reached its monitoring deadline. Queued or running workflows "
                      "are still pending; this deadline is not a test timeout.", ""])
    lines.extend(["Automatic approval was withheld until every requested workflow completes successfully.", ""])
    if not results:
        lines.append("No requested workflows were provided.")
    for kind, heading in (("failed", "Failed workflows"), ("nonpassing", "Other non-passing workflows"),
                          ("pending", "Pending workflows"), ("unverified", "Unverified workflows")):
        if not groups[kind]:
            continue
        lines.extend([f"### {heading}", ""])
        for result in groups[kind]:
            name = result["name"]
            lines.append(f"**[{name}]({result['url']})**" if "run_id" in result else f"**{name}**")
            lines.append(f"- Status: {result.get('status') or 'unverified'}; "
                         f"conclusion: {result.get('conclusion') or 'not reported'}.")
            if kind == "failed" and result.get("status") in ACTIVE:
                lines.append("- Other jobs in this workflow are still pending.")
            for field in ("error", "job_error"):
                if result.get(field):
                    lines.append(f"- {result[field]}")
            for job in result.get("jobs", []):
                if job.get("conclusion") in FAILURES:
                    lines.extend(failed_job_lines(job, result["key"], request))
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-wait", type=float, default=7200)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--report", type=Path, default=Path("ci-results.md"))
    args = parser.parse_args(argv)
    if args.max_wait < 0 or args.interval <= 0:
        parser.error("max-wait must be nonnegative and interval must be positive")
    results, expired = wait_for_results(requested_workflows(os.environ),
                                        max_wait=args.max_wait, interval=args.interval)
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        output.write(f"all_workflows_passed={str(all_passed(results)).lower()}\n")
    if not all_passed(results):
        inspect_jobs(results)
        args.report.write_text(render_report(results, os.environ["PR_NUMBER"], expired))


if __name__ == "__main__":
    main()
