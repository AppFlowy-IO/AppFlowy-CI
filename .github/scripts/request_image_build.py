#!/usr/bin/env python3
"""Build one pinned Cloud revision in the private runner repository."""

import json
import os
import re
import signal
import subprocess
import time
from urllib.parse import quote


REPOSITORY = "AppFlowy-IO/AppFlowy-Cloud-Premium"
WORKFLOW = "build_ci_images_self_hosted.yml"
API_ROOT = f"repos/{REPOSITORY}/actions"
ARTIFACTS = {f"docker-image-{service}" for service in ("cloud", "worker", "search", "mcp")}


def safe_diagnostic(value):
    """Keep only a bounded, single-line diagnostic; never print response headers wholesale."""
    text = str(value)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ADMIN_GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            text = text.replace(token, "[redacted]")
    return " ".join(text.split())[:240]


class GitHubAPIError(RuntimeError):
    """A failed observation retains the evidence needed to distinguish pressure from bad access."""

    def __init__(self, method, path, *, status=None, headers=None, message="", transport=False):
        allowed = {"retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "x-github-request-id"}
        self.headers = {k.lower(): safe_diagnostic(v) for k, v in (headers or {}).items()
                        if k.lower() in allowed}
        message = str(message)
        self.status = status
        self.transport = transport
        self.rate_limited = status == 429 or (status == 403 and (
            self.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in self.headers
            or "secondary rate limit" in message.lower()
        ))
        self.retryable = self.rate_limited or transport or (
            status is not None and 500 <= status < 600
        )
        self.method = method
        self.path = path
        detail = safe_diagnostic(message)
        evidence = ", ".join(f"{k}={v}" for k, v in sorted(self.headers.items()))
        super().__init__(f"GitHub API {method} {path} failed "
                         f"(HTTP {status if status is not None else 'unavailable'}; {evidence}): {detail}")

    def retry_delay(self, attempt, wall_time):
        if not self.rate_limited:
            return 2 * (2 ** attempt)
        # GitHub requires at least a minute for a secondary limit without an explicit hint.
        delays = []
        retry_after = self.headers.get("retry-after", "")
        if re.fullmatch(r"[0-9]+", retry_after):
            delays.append(int(retry_after))
        reset = self.headers.get("x-ratelimit-reset", "")
        if self.headers.get("x-ratelimit-remaining") == "0" and re.fullmatch(r"[0-9]+", reset):
            delays.append(max(0, int(reset) - wall_time + 1))
        return max([1, *delays]) if delays else 60 * (2 ** attempt)


class ObservationUnavailable(RuntimeError):
    """The caller could not observe its build; this is not proof that the build should stop."""


class BuildWaitTimeout(TimeoutError):
    """The build's explicit overall wait, rather than an individual API read, expired."""


class BuildProvenanceError(RuntimeError):
    """The correlated private build does not match the requested immutable provenance."""


def api_once(method, path, payload=None, *, timeout=30):
    # A mutation is attempted exactly once. The caller owns retry/deadline policy for reads.
    command = ["gh", "api", "--include", "--method", method, path]
    if payload is not None:
        command += ["--input", "-"]
    try:
        result = subprocess.run(
            command, input=json.dumps(payload) if payload is not None else None,
            text=True, capture_output=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise GitHubAPIError(method, path, message="request timed out", transport=True) from error
    response = result.stdout.replace("\r\n", "\n")
    head, separator, body = response.partition("\n\n")
    match = re.match(r"HTTP/\S+ ([0-9]{3})(?: |$)", head.split("\n", 1)[0])
    status = int(match.group(1)) if match and separator else None
    headers = {}
    if status is not None:
        for line in head.split("\n")[1:]:
            key, colon, value = line.partition(":")
            if colon:
                headers[key.lower()] = value.strip()
    else:
        body = response
    if result.returncode or (status is not None and status >= 400):
        try:
            message = json.loads(body).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        # gh transport failures have no HTTP status. Do not treat a missing credential/CLI as
        # a transient failure; only known network failures receive bounded read retries.
        network = status is None and bool(re.search(
            r"timeout|timed out|connection reset|connection refused|unexpected EOF|"
            r"TLS handshake|network is unreachable|no such host|dial tcp", result.stderr, re.I,
        ))
        if not message:
            message = "network request failed" if network else "gh request failed without an HTTP response"
        raise GitHubAPIError(method, path, status=status, headers=headers,
                             message=message, transport=network)
    if status is None:
        raise GitHubAPIError(method, path, message="missing HTTP status in gh response")
    try:
        return json.loads(body) if body.strip() else None
    except ValueError as error:
        raise GitHubAPIError(method, path, status=status,
                             message="invalid JSON response") from error


def read_with_retry(request, path, deadline, wait, now, wall_time, *, attempts=4):
    """All GET attempts, waits and process time share the caller's one deadline."""
    last_error = None
    for attempt in range(attempts):
        remaining = deadline - now()
        if remaining <= 0:
            raise ObservationUnavailable(f"Observation deadline reached; last error: {last_error}")
        try:
            result = (request("GET", path) if request is not None else
                      api_once("GET", path, timeout=min(30, remaining)))
            if now() >= deadline:
                raise ObservationUnavailable("GitHub read completed after the observation deadline")
            return result
        except GitHubAPIError as error:
            if not error.retryable:
                raise
            last_error = error
            if attempt + 1 == attempts:
                raise ObservationUnavailable(f"GET retry limit reached: {error}") from error
            delay = error.retry_delay(attempt, wall_time())
            print(f"GitHub observation delayed by {delay:.0f}s: {error}", flush=True)
            wait(min(delay, max(0, deadline - now())))
    raise AssertionError("bounded read loop exhausted unexpectedly")


def api(method, path, payload=None):
    """Keep the artifact downloader's bounded three-read retry contract."""
    if method == "GET":
        return read_with_retry(None, path, time.monotonic() + 94,
                               time.sleep, time.monotonic, time.time, attempts=3)
    return api_once(method, path, payload)


def find_run(request, title):
    # Correlation includes the caller's run attempt and source SHA. Never select
    # the most recent run: another CI request may dispatch at the same time.
    runs = request("GET", f"{API_ROOT}/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100")
    matches = [run for run in runs["workflow_runs"] if run["display_title"] == title]
    if len(matches) > 1:
        raise RuntimeError("Multiple image builds have the same request ID; refusing to guess")
    return matches[0] if matches else None


def request_build(source_sha, request_id, request=None, wait=time.sleep,
                  now=time.monotonic, timeout=330 * 60, *, wall_time=time.time, ci_tools_sha=None,
                  test_features=None, test_rust_toolchain=None, pr_number=None,
                  image_workflow_ref="main"):
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("Expected an immutable 40-character Cloud commit")
    if not re.fullmatch(r"[0-9]+-[0-9]+", request_id):
        raise ValueError("Expected the AppFlowy-CI run ID and attempt")
    if (
        not isinstance(image_workflow_ref, str)
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}", image_workflow_ref)
        or re.fullmatch(r"[a-f0-9]{40}", image_workflow_ref)
        or subprocess.run(
            ["git", "check-ref-format", "refs/heads/" + image_workflow_ref],
            capture_output=True, check=False,
        ).returncode
    ):
        raise ValueError("Expected a Cloud image-builder branch or tag name")
    inputs = {"source_sha": source_sha, "request_id": request_id}
    if pr_number:
        if not re.fullmatch(r"[1-9][0-9]*", pr_number):
            raise ValueError("Expected a positive Cloud pull request number")
        inputs["pr_number"] = pr_number
    required_artifacts = set(ARTIFACTS)
    if ci_tools_sha:
        if not re.fullmatch(r"[a-f0-9]{40}", ci_tools_sha):
            raise ValueError("Expected an immutable AppFlowy-CI tools commit")
        if not re.fullmatch(r"[a-zA-Z0-9,_-]+", test_features or ""):
            raise ValueError("Expected the Cloud test Cargo features")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", test_rust_toolchain or ""):
            raise ValueError("Expected a pinned Rust toolchain version")
        inputs.update(ci_tools_sha=ci_tools_sha, test_features=test_features,
                      test_rust_toolchain=test_rust_toolchain)
        required_artifacts.add(f"cloud-test-binaries-{source_sha}")
    started = now()
    deadline = started + timeout

    def read(path, *, until=None):
        return read_with_retry(request, path, min(deadline, until) if until is not None else deadline,
                               wait, now, wall_time)

    def call(method, path, payload=None):
        if method == "GET":
            return read(path)
        return (request(method, path, payload) if request is not None else
                api_once(method, path, payload, timeout=min(30, max(0.001, deadline - now()))))

    # Main remains the trusted default for ordinary requests. A manual pre-merge
    # override must use the tested Cloud commit for both tools and application.
    workflow_sha = None
    if image_workflow_ref != "main":
        revision = call(
            "GET", f"repos/{REPOSITORY}/commits/{quote(image_workflow_ref, safe='')}",
        )
        workflow_sha = revision.get("sha")
        if workflow_sha != source_sha:
            raise ValueError("The image-builder override must identify the tested Cloud commit")
    title = f"CI images {request_id} ({source_sha})"
    if pr_number:
        title += f" [PR #{pr_number}]"
    run = find_run(call, title)
    submitted = run is not None
    cancel_owned = False
    try:
        if run is None:
            submitted = True
            call("POST", f"{API_ROOT}/workflows/{WORKFLOW}/dispatches", {
                "ref": image_workflow_ref, "inputs": inputs,
            })
        discovery_deadline = min(deadline, now() + 180)
        while run is None and now() < discovery_deadline:
            run = find_run(lambda method, path: read(path, until=discovery_deadline), title)
            if run is None:
                wait(min(5, max(0, discovery_deadline - now())))
        if run is None:
            raise BuildWaitTimeout("GitHub did not expose the dispatched image build within 3 minutes")
        if workflow_sha is not None and run.get("head_sha") != workflow_sha:
            raise BuildProvenanceError("Dispatched image-builder revision differs from the tested Cloud commit")

        run_id = int(run["id"])
        builder_sha = run["head_sha"]
        url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
        print(f"Waiting for {url}", flush=True)
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            description = "Images and compiled Cloud tests" if ci_tools_sha else "Images"
            pr_label = f" for PR #{pr_number}" if pr_number else ""
            print(f"{description}{pr_label} for {source_sha}: [self-hosted build]({url}).", file=summary)
            print(f"Image-builder workflow: `{image_workflow_ref}` at `{run['head_sha']}`.", file=summary)
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
                raise BuildWaitTimeout(f"Timed out waiting for {url}")
            wait(min(15, deadline - current))
            if now() >= deadline:
                raise BuildWaitTimeout(f"Timed out waiting for {url}")
            observed = read(f"{API_ROOT}/runs/{run_id}")
            if (observed.get("id") != run_id or observed.get("display_title") != title
                    or observed.get("head_sha") != builder_sha):
                raise BuildProvenanceError("Observed image build changed its request/source provenance")
            run = observed
        if run["conclusion"] != "success":
            raise RuntimeError(f"Image build {run['conclusion']}: {url}")
        artifacts = read(f"{API_ROOT}/runs/{run_id}/artifacts?per_page=100")
        available = {item["name"] for item in artifacts["artifacts"] if not item["expired"]}
        if not required_artifacts.issubset(available):
            raise RuntimeError(f"Private build is missing artifacts: {sorted(required_artifacts - available)}")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"run_id={run_id}", file=output)
            print(f"image_workflow_ref={image_workflow_ref}", file=output)
            print(f"image_workflow_sha={run['head_sha']}", file=output)
        return run_id
    except (InterruptedError, BuildWaitTimeout, BuildProvenanceError):
        cancel_owned = True
        raise
    except (GitHubAPIError, ObservationUnavailable):
        if submitted:
            print(f"::warning::Observation failed; preserving correlated private build for {title}",
                  flush=True)
        raise
    finally:
        # Cancellation belongs only to explicit interruption/deadline/provenance rejection.
        # A failed read is not evidence that a queued or running builder should be stopped.
        if cancel_owned and submitted and (run is None or run["status"] != "completed"):
            try:
                cleanup_request = request or api_once
                run = run or find_run(cleanup_request, title)
                if run is not None and run["status"] != "completed":
                    cleanup_request("POST", f"{API_ROOT}/runs/{int(run['id'])}/cancel")
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
        ci_tools_sha=os.environ.get("CI_TOOLS_SHA"),
        test_features=os.environ.get("CLOUD_TEST_FEATURES"),
        test_rust_toolchain=os.environ.get("RUST_TOOLCHAIN"),
        pr_number=os.environ.get("PR_NUMBER") or None,
        image_workflow_ref=os.environ.get("IMAGE_WORKFLOW_REF") or "main",
    )


if __name__ == "__main__":
    main()
