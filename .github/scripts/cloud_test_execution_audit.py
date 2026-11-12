#!/usr/bin/env python3
"""Audit PR-added Rust test functions against same-source CI execution, without running PR code.

Standalone use: report --source PATH --changes compare.json --logs LOG_DIRECTORY --sha SHA.
The default workflow audits new/renamed test functions and changed test attributes. --all-tests
also inventories legacy tests (including feature/platform-only cases); it is advisory unless
--strict is supplied. This is execution accounting, not LLVM line/branch coverage. Generated
parameterized cases require a libtest inventory and are reported as unverified, never passed.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

from ci_run_lifecycle import CLOUD_REPO, CI_REPO, identity

SHA = re.compile(r"[a-f0-9]{40}")
RAW = re.compile(r'(?:br|cr|r)(#{0,255})"')
CHAR = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\n])'")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FUNCTION = re.compile(r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+((?:r#)?[A-Za-z_][A-Za-z_0-9]*)\s*\(")
CONTEXT = "CI test execution audit"
WORKFLOWS = {"cloud_integration_ci.yaml", "cloud_commercial_integration_ci.yaml"}


def mask_literals(source):
    """Retain offsets/newlines while masking Rust strings, chars and nested comments."""
    result = list(source)
    position = 0
    while position < len(source):
        start = position
        if source.startswith("//", position):
            end = source.find("\n", position)
            position = len(source) if end < 0 else end
        elif source.startswith("/*", position):
            position += 2
            depth = 1
            while depth and position < len(source):
                if source.startswith("/*", position):
                    depth += 1
                    position += 2
                elif source.startswith("*/", position):
                    depth -= 1
                    position += 2
                else:
                    position += 1
            if depth:
                raise ValueError("Unterminated Rust comment")
        elif source[position] in "brc" and (match := RAW.match(source, position)):
            terminator = '"' + match.group(1)
            end = source.find(terminator, match.end())
            if end < 0:
                raise ValueError("Unterminated Rust raw string")
            position = end + len(terminator)
        elif source[position] == '"':
            position += 1
            while position < len(source) and source[position] != '"':
                position += 2 if source[position] == "\\" else 1
            if position >= len(source):
                raise ValueError("Unterminated Rust string")
            position += 1
        elif source[position] == "'" and (match := CHAR.match(source, position)):
            position = match.end()
        else:
            position += 1
            continue
        result[start:position] = ("\n" if char == "\n" else " " for char in source[start:position])
    return "".join(result)


def functions(source, filename):
    code = mask_literals(source)
    cursor = 0
    while (start := code.find("#", cursor)) >= 0:
        cursor = start
        attributes = []
        while match := re.match(r"#\s*\[", code[cursor:]):
            end = cursor + match.end()
            depth = 1
            while depth and end < len(code):
                depth += (code[end] == "[") - (code[end] == "]")
                end += 1
            if depth:
                raise ValueError("Unterminated Rust attribute: " + filename)
            attributes.append(code[cursor:end])
            cursor = end
            while cursor < len(code) and code[cursor].isspace():
                cursor += 1
        if not attributes:
            cursor = start + 1
            continue
        names = [re.match(r"#\s*\[\s*([A-Za-z_][A-Za-z_0-9:]*)", attribute)
                 for attribute in attributes]
        names = [match[1].split("::")[-1] for match in names if match]
        conditional = any(re.search(r"#\s*\[\s*cfg_attr\b.*\btest\b", attribute)
                          for attribute in attributes)
        match = FUNCTION.match(code, cursor)
        if match and (conditional or set(names) & {"test", "rstest", "test_case"}):
            name = match[1]
            yield {"id": filename + "::" + name, "file": filename, "name": name,
                   "line": code.count("\n", 0, match.start(1)) + 1,
                   "attribute_line": code.count("\n", 0, start) + 1,
                   "parameterized": conditional or name.startswith("r#") or bool(set(names) & {"rstest", "test_case"})}


def added_lines(patch):
    lines = set()
    number = None
    for line in patch.splitlines():
        if line.startswith("@@ "):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if not match:
                raise ValueError("Malformed diff hunk")
            number = int(match[1])
        elif number is not None:
            if line.startswith("+"):
                lines.add(number)
                number += 1
            elif line.startswith(" "):
                number += 1
    return lines


def inventory(source, changes, all_tests=False):
    files = subprocess.check_output(["git", "ls-files", "-z", "--", "*.rs"], cwd=source)
    declared = []
    for filename in files.decode().split("\0"):
        if not filename:
            continue
        path = (source / filename).resolve()
        if not path.is_relative_to(source.resolve()):
            raise ValueError("Rust source escapes checkout: " + filename)
        if path.is_file():
            declared.extend(functions(path.read_text(), filename))
    counts = Counter(case["name"] for case in declared)
    touched = {}
    for entry in changes.get("files", []):
        if entry["filename"].endswith(".rs") and entry.get("status") != "removed":
            if "patch" not in entry:
                raise ValueError("Missing/truncated Rust diff: " + entry["filename"])
            touched[entry["filename"]] = added_lines(entry["patch"])
    selected = [case for case in declared if all_tests or any(
        line in touched.get(case["file"], ())
        for line in range(case["attribute_line"], case["line"] + 1))]
    for case in selected:
        case["ambiguous"] = counts[case["name"]] != 1
    return selected


def executions(log, sha):
    """Credit only complete, count-consistent libtest segments with exact source attestation."""
    text = ANSI.sub("", log).replace("\ufeff", "")
    markers = set(re.findall(r"AF_TEST_SOURCE_SHA=([a-f0-9]{40})", text))
    if markers != {sha}:
        return {}, ["Missing or mixed source attestation"]
    observed, current, pending = {}, None, None
    issues = []
    for raw in text.splitlines():
        line = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", raw).strip()
        if re.fullmatch(r"running \d+ tests?", line):
            current, pending = {}, None
        if current is None:
            continue
        match = re.match(r"test ([A-Za-z_0-9:]+) \.\.\.\s*(ok|FAILED|ignored)?", line)
        if match:
            pending = match[1]
            if match[2]:
                current[pending] = match[2]
                pending = None
        elif pending and line in {"ok", "FAILED", "ignored"}:
            current[pending] = line
            pending = None
        summary = re.match(r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;", line)
        if summary:
            expected = dict(zip(("ok", "FAILED", "ignored"), map(int, summary.groups())))
            actual = Counter(current.values())
            if all(actual[name] == count for name, count in expected.items()):
                for name, state in current.items():
                    # A later ignored delivery never erases an actual execution of the same test.
                    if state != "ignored" or name not in observed:
                        observed[name] = state
            else:
                issues.append("Libtest summary did not match parsed case outcomes")
            current, pending = None, None
    if current:
        issues.append("Incomplete libtest segment")
    return observed, issues


def audit(source, changes, logs, sha, *, all_tests=False):
    if not SHA.fullmatch(sha):
        raise ValueError("An immutable source SHA is required")
    cases = inventory(source, changes, all_tests)
    evidence = {}
    issues = []
    for label, log in (logs.items() if isinstance(logs, dict) else logs):
        records, errors = executions(log, sha)
        issues.extend({"log": label, "reason": message} for message in errors)
        for name, state in records.items():
            evidence.setdefault(name.rsplit("::", 1)[-1], []).append({"name": name, "state": state, "log": label})
    exceptions_path = source / "script/ci/test_execution_exemptions.json"
    if not exceptions_path.resolve().is_relative_to(source.resolve()):
        raise ValueError("Execution exemptions escape the source checkout")
    exceptions = json.loads(exceptions_path.read_text()) if exceptions_path.exists() else {}
    if not isinstance(exceptions, dict) or any(not isinstance(reason, str) or len(reason.strip()) < 15
                                              for reason in exceptions.values()):
        raise ValueError("Execution exemptions require exact case IDs and explanatory reasons")
    for case in cases:
        case["evidence"] = evidence.get(case["name"], [])
        outcomes = {entry["state"] for entry in case["evidence"]}
        if case["id"] in exceptions:
            case.update(status="exempt", reason=exceptions[case["id"]])
        elif case["ambiguous"] or case["parameterized"]:
            case["status"] = "unverified_identity"
        elif "FAILED" in outcomes:
            case["status"] = "failed"
        elif "ok" in outcomes:
            case["status"] = "executed"
        elif "ignored" in outcomes:
            case["status"] = "ignored_only"
        else:
            case["status"] = "not_observed"
    return {"schema_version": 1, "source_sha": sha, "scope": "all Rust test functions" if all_tests else
            "PR-added test functions and changed test attributes", "cases": cases,
            "counts": dict(Counter(case["status"] for case in cases)), "log_issues": issues}


def summarize(report):
    gaps = [case for case in report["cases"] if case["status"] not in {"executed", "exempt"}]
    lines = ["## CI test execution audit", "", f"Source: `{report['source_sha']}`", "",
             f"Scope: {report['scope']}", "", f"Outcomes: `{json.dumps(report['counts'], sort_keys=True)}`", ""]
    if report.get("pending_runs"):
        lines.append("Related CI workflows are still running; missing evidence is pending.")
    for case in gaps[:30]:
        message = f"{case['status']}: {case['id']}"
        if not report.get("pending_runs"):
            print(f"::warning file={case['file']},line={case['line']}::{message}")
        lines.append(f"- `{case['file']}:{case['line']}` — **{case['status']}**: `{case['name']}`")
    if len(gaps) > 30:
        lines.append(f"Additional gaps in the JSON report: {len(gaps) - 30}")
    for case in report["cases"]:
        if case["status"] == "exempt":
            lines.append(f"- Explicit exception `{case['id']}`: {case['reason']}")
    summary = "\n".join(lines) + "\n"
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write(summary)
    return bool(gaps)


def api(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


def job_log(job_id, repository=CI_REPO):
    # GitHub returns a signed download URL. Never forward the repository token to that host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, *args, **kwargs):
            return None

    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}/logs",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json"})
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=60)
    except urllib.error.HTTPError as error:
        if error.code != 302:
            raise
        location = error.headers["Location"]
        if not location.startswith("https://"):
            raise ValueError("GitHub returned a non-HTTPS log URL")
        response = urllib.request.urlopen(urllib.request.Request(location), timeout=60)
    with response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Job log exceeded the 32 MiB audit bound")
    return data.decode("utf-8", errors="replace")


def related_runs(pr_number):
    runs = []
    for workflow in sorted(WORKFLOWS):
        found = None
        for page in range(1, 4):
            batch = api(f"repos/{CI_REPO}/actions/workflows/{workflow}/runs?per_page=100&page={page}")["workflow_runs"]
            found = next((run for run in batch if (info := identity(run))
                          and info[:2] == (CLOUD_REPO, pr_number)), None)
            if found or len(batch) < 100:
                break
        if found is None:
            raise ValueError("No related CI run found within the bounded history: " + workflow)
        runs.append(found)
    return runs


def attest_private_log(text, run, sha, merges):
    """Native GitHub PR jobs may test a synthetic merge; verify its private-head parent.

    Explicit attestations are never overridden. Older private workflows are accepted only from
    the matching API head and a recorded private checkout, not an arbitrary SHA in build output.
    """
    if "AF_TEST_SOURCE_SHA=" in text:
        return text, None
    repository = None
    expect_sha = False
    for raw in ANSI.sub("", text).splitlines():
        line = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", raw).strip()
        match = re.fullmatch(r"repository: (AppFlowy-IO/[A-Za-z0-9_-]+)", line)
        if match:
            repository = match[1]
        if expect_sha:
            expect_sha = False
            if SHA.fullmatch(line):
                valid = line == sha
                if not valid and run.get("event") == "pull_request":
                    if line not in merges:
                        merges[line] = api(f"repos/{CLOUD_REPO}/git/commits/{line}")
                    parents = [parent["sha"] for parent in merges[line].get("parents", [])]
                    valid = len(parents) == 2 and parents[1] == sha
                if valid:
                    return "AF_TEST_SOURCE_SHA=" + sha + "\n" + text, {
                        "checkout_sha": line, "kind": "exact_private_checkout" if line == sha else "github_pr_test_merge"}
        if repository == CLOUD_REPO and re.search(r"git log -1 --format=%H$", line):
            expect_sha = True
    return text, None


def publish(pr_number, sha, state, description):
    pr = api(f"repos/{CLOUD_REPO}/pulls/{pr_number}")
    if pr["state"] != "open" or pr["head"]["sha"] != sha:
        print("PR closed or advanced; leaving its current status untouched")
        return
    payload = {"state": state, "context": CONTEXT, "description": description[:140],
               "target_url": f"https://github.com/{CI_REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}"}
    subprocess.run(["gh", "api", "--method", "POST", f"repos/{CLOUD_REPO}/statuses/{sha}",
                    "--input", "-"], input=json.dumps(payload), text=True, check=True,
                   stdout=subprocess.DEVNULL, timeout=60)


def github_report(source, pr_number, sha, output, publish_status=False):
    pr = api(f"repos/{CLOUD_REPO}/pulls/{pr_number}")
    if pr["state"] != "open" or pr["head"]["sha"] != sha:
        print("PR closed or advanced; this audit is superseded")
        return 0
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if actual != sha:
        raise ValueError("Audit checkout differs from the requested source revision")
    changes = api(f"repos/{CLOUD_REPO}/compare/{pr['base']['sha']}...{sha}")
    if len(changes.get("files", [])) >= 300:
        raise ValueError("Changed-file inventory may exceed GitHub's 300-file bound")
    runs = [dict(run, audit_repository=CI_REPO) for run in related_runs(pr_number)]
    private = api(f"repos/{CLOUD_REPO}/actions/runs?head_sha={sha}&per_page=100")
    if private["total_count"] > 100:
        raise ValueError("Private same-source run inventory exceeded the 100-run bound")
    runs.extend(dict(run, audit_repository=CLOUD_REPO) for run in private["workflow_runs"]
                if run["head_sha"] == sha)
    checkout_proofs, merges = [], {}

    def logs():
        total = 0
        for run in runs:
            for page in range(1, 4):
                batch = api(f"repos/{run['audit_repository']}/actions/runs/{run['id']}/jobs?per_page=100&page={page}")
                if batch["total_count"] > 300:
                    raise ValueError("CI job inventory exceeded the 300-job bound")
                for job in batch["jobs"]:
                    if job["status"] == "completed" and job["conclusion"] != "skipped":
                        try:
                            text = job_log(job["id"], run["audit_repository"])
                        except (OSError, ValueError) as error:
                            # Signed URLs can contain tokens. Report stable job IDs only.
                            raise ValueError(f"Cannot read audit input for job {job['id']}: {type(error).__name__}") from None
                        text, proof = attest_private_log(text, run, sha, merges)
                        if proof:
                            checkout_proofs.append(dict(proof, job=job["html_url"]))
                        total += len(text.encode())
                        if total > 256 * 1024 * 1024:
                            raise ValueError("CI log collection exceeded the 256 MiB audit bound")
                        yield job["html_url"], text
                if len(batch["jobs"]) < 100:
                    break
    report = audit(source, changes, logs(), sha)
    report["private_checkout_proofs"] = checkout_proofs
    report["ci_runs"] = [{"url": run["html_url"], "status": run["status"],
                          "workflow_sha": run["head_sha"], "attempt": run["run_attempt"]} for run in runs]
    report["pending_runs"] = [run["html_url"] for run in runs if run["status"] != "completed"]
    output.write_text(json.dumps(report, indent=2) + "\n")
    gaps = summarize(report)
    state = "pending" if report["pending_runs"] else "failure" if gaps else "success"
    description = ("Waiting for related CI workflows" if state == "pending" else
                   "New Rust tests lack verified execution; see the audit" if gaps else
                   f"Verified execution of {len(report['cases'])} new Rust test functions")
    if publish_status:
        publish(pr_number, sha, state, description)
    return int(state == "failure")


def resolve(event):
    if "client_payload" in event:
        payload = event["client_payload"]
        if (payload.get("event_type") not in {"pr_opened", "pr_sync"}
                or not re.fullmatch(r"[1-9][0-9]*", str(payload.get("pr_number", "")))
                or not SHA.fullmatch(payload.get("source_sha", ""))):
            raise ValueError("Audit dispatch requires a private PR and immutable source SHA")
        pr = api(f"repos/{CLOUD_REPO}/pulls/{payload['pr_number']}")
        return pr if pr["state"] == "open" and pr["head"]["sha"] == payload["source_sha"] else None
    info = identity(event["workflow_run"])
    if not info or info[0] != CLOUD_REPO or not info[1]:
        return None
    pr = api(f"repos/{CLOUD_REPO}/pulls/{info[1]}")
    return pr if pr["state"] == "open" else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--event", required=True, type=Path)
    github_parser = sub.add_parser("github")
    github_parser.add_argument("--source", required=True, type=Path)
    github_parser.add_argument("--pr", required=True, type=int)
    github_parser.add_argument("--sha", required=True)
    github_parser.add_argument("--output", required=True, type=Path)
    github_parser.add_argument("--publish", action="store_true")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--pr", required=True, type=int)
    status_parser.add_argument("--sha", required=True)
    status_parser.add_argument("--state", choices=["pending", "failure"], required=True)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--source", required=True, type=Path)
    report_parser.add_argument("--sha", required=True)
    report_parser.add_argument("--changes", required=True, type=Path)
    report_parser.add_argument("--logs", required=True, type=Path)
    report_parser.add_argument("--output", required=True, type=Path)
    report_parser.add_argument("--all-tests", action="store_true")
    report_parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if args.mode == "status":
        if not SHA.fullmatch(args.sha):
            raise ValueError("Invalid source revision")
        publish(args.pr, args.sha, args.state, "Audit running" if args.state == "pending" else
                "Test execution audit failed or could not verify its inputs")
        return 0
    if args.mode == "github":
        if not SHA.fullmatch(args.sha):
            raise ValueError("Invalid source revision")
        return github_report(args.source.resolve(), args.pr, args.sha, args.output, args.publish)
    if args.mode == "resolve":
        pr = resolve(json.loads(args.event.read_text()))
        values = {"enabled": "false"} if pr is None else {
            "enabled": "true", "pr": str(pr["number"]), "sha": pr["head"]["sha"], "base": pr["base"]["sha"]}
        if pr and not all(SHA.fullmatch(values[key]) for key in ("sha", "base")):
            raise ValueError("Invalid source revision")
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
        return 0
    report = audit(args.source.resolve(), json.loads(args.changes.read_text()),
                   {path.name: path.read_text() for path in args.logs.glob("*.log")},
                   args.sha, all_tests=args.all_tests)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return int(summarize(report) and args.strict)


if __name__ == "__main__":
    sys.exit(main())
