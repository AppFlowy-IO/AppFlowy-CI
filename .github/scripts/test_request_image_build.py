"""Test the private build handoff without dispatching GitHub workflows."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import request_image_build as build


SHA = "a" * 40
MAIN_WORKFLOW_SHA = "c" * 40
REQUEST_ID = "1234-2"


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.run = None
        self.conclusion = "success"
        self.artifacts = [{"name": name, "expired": False} for name in build.ARTIFACTS]
        self.elapsed = 0
        self.private = True
        self.override_sha = SHA
        self.dispatched_sha = None

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path.endswith("/dispatches"):
            pr_number = (payload or {}).get("inputs", {}).get("pr_number")
            suffix = f" [PR #{pr_number}]" if pr_number else ""
            revision = f"[{SHA}]" if (payload or {}).get("inputs", {}).get("commercial") == "true" else f"({SHA})"
            self.run = {
                "id": 42, "status": "queued", "conclusion": None,
                "display_title": f"CI images {REQUEST_ID} {revision}{suffix}",
                "head_sha": self.dispatched_sha or (
                    MAIN_WORKFLOW_SHA if (payload or {}).get("ref", "main") == "main" else self.override_sha
                ),
            }
        elif method == "POST" and path.endswith("/cancel"):
            return None
        elif "/workflows/" in path and "/runs?" in path:
            unrelated = {"id": 999, "display_title": f"CI images 9999-1 ({SHA})"}
            return {"workflow_runs": [unrelated] + ([dict(self.run)] if self.run else [])}
        elif "/artifacts?" in path:
            return {"artifacts": self.artifacts}
        elif path.endswith("/runs/42"):
            self.run["status"] = "completed" if self.conclusion else "in_progress"
            self.run["conclusion"] = self.conclusion
            return dict(self.run)
        elif path == f"repos/{build.REPOSITORY}":
            return {"private": self.private}
        elif path.startswith(f"repos/{build.REPOSITORY}/commits/"):
            return {"sha": self.override_sha}
        else:
            raise AssertionError(f"Unexpected API call: {method} {path}")

    def wait(self, seconds):
        self.elapsed += seconds

    @property
    def writes(self):
        return [call for call in self.calls if call[0] == "POST"]


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "output"
        env = patch.dict(os.environ, {
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(Path(self.directory.name) / "summary"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.github = FakeGitHub()

    def request(self, **kwargs):
        return build.request_build(
            SHA, REQUEST_ID, request=self.github.request,
            now=lambda: self.github.elapsed, wait=kwargs.pop("wait", self.github.wait), **kwargs,
        )

    def test_commercial_dispatch_requires_private_artifacts_with_distinct_provenance(self):
        self.github.private = False
        with self.assertRaisesRegex(ValueError, "private producer"):
            self.request(commercial=True, lock_published_images=True)
        self.assertFalse(self.github.writes)
        self.github.private = True
        with self.assertRaisesRegex(RuntimeError, "missing artifacts"):
            self.request(commercial=True, lock_published_images=True)
        self.github.artifacts = [{"name": n, "expired": False} for n in
                                 ("commercial-cloud-images", "commercial-service-images",
                                  f"private-image-lock-{REQUEST_ID}")]
        self.assertEqual(self.request(commercial=True, lock_published_images=True), 42)
        self.assertIn(f"[{SHA}]", self.github.run["display_title"])
        self.assertIn(f"lock_artifact=private-image-lock-{REQUEST_ID}", self.output.read_text())
        self.assertEqual(len(self.github.writes), 1)

    def test_dispatches_pinned_revision_and_ignores_other_requests(self):
        self.assertEqual(self.request(), 42)
        self.assertEqual(self.output.read_text(), (
            "run_id=42\nimage_workflow_ref=main\n"
            f"image_workflow_sha={MAIN_WORKFLOW_SHA}\n"
        ))
        self.assertEqual(len(self.github.writes), 1)
        self.assertEqual(self.github.writes[0][2], {
            "ref": "main", "inputs": {"source_sha": SHA, "request_id": REQUEST_ID},
        })

    def test_manual_builder_override_uses_same_source_commit_and_records_provenance(self):
        self.assertEqual(self.request(image_workflow_ref="fix/workspace-import-publication"), 42)
        self.assertEqual(self.github.writes[0][2], {
            "ref": "fix/workspace-import-publication",
            "inputs": {"source_sha": SHA, "request_id": REQUEST_ID},
        })
        self.assertIn(("GET", f"repos/{build.REPOSITORY}/commits/fix%2Fworkspace-import-publication", None),
                      self.github.calls)
        self.assertEqual(self.output.read_text(), (
            "run_id=42\nimage_workflow_ref=fix/workspace-import-publication\n"
            f"image_workflow_sha={SHA}\n"
        ))
        summary = Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
        self.assertIn(f"`fix/workspace-import-publication` at `{SHA}`", summary)
        self.assertEqual(self.github.run["display_title"], f"CI images {REQUEST_ID} ({SHA})")

    def test_builder_override_must_resolve_to_the_tested_cloud_commit_before_dispatch(self):
        self.github.override_sha = "b" * 40
        with self.assertRaisesRegex(ValueError, "tested Cloud commit"):
            self.request(image_workflow_ref="fix/other-source")
        self.assertFalse(self.github.writes)
        self.assertFalse(self.output.exists())

    def test_override_branch_movement_rejects_and_cancels_only_its_correlated_build(self):
        self.github.dispatched_sha = "b" * 40
        with self.assertRaisesRegex(RuntimeError, "revision differs"):
            self.request(image_workflow_ref="fix/workspace-import-publication")
        self.assertEqual(self.github.writes[-1][:2], ("POST", f"{build.API_ROOT}/runs/42/cancel"))
        self.assertFalse(self.output.exists())

    def test_invalid_builder_refs_cannot_read_or_dispatch(self):
        for reference in ("", "../main", "fix//import", "fix/import.lock", "main\nother",
                          "https://other.example/main", "-option", SHA):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                self.request(image_workflow_ref=reference)
        self.assertFalse(self.github.calls)

    @patch.object(build, "request_build")
    def test_legacy_entrypoint_defaults_to_main_and_forwards_explicit_environment(self, request):
        with patch.object(build.signal, "signal"), patch.dict(os.environ, {
            "SOURCE_SHA": SHA, "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "2",
            "IMAGE_WORKFLOW_REF": "",
        }, clear=True):
            build.main()
            self.assertEqual(request.call_args.kwargs["image_workflow_ref"], "main")
            os.environ["IMAGE_WORKFLOW_REF"] = "fix/workspace-import-publication"
            build.main()
            self.assertEqual(request.call_args.kwargs["image_workflow_ref"],
                             "fix/workspace-import-publication")

    def test_same_private_run_builds_images_and_pinned_test_binaries(self):
        tools = {"ci_tools_sha": "b" * 40, "test_features": "ai-test-enabled,sync-v2,ci-test",
                 "test_rust_toolchain": "1.98.0"}
        self.github.artifacts.append({"name": f"cloud-test-binaries-{SHA}", "expired": False})
        self.assertEqual(self.request(**tools), 42)
        self.assertEqual(len(self.github.writes), 1)
        self.assertEqual(self.github.writes[0][2]["inputs"], {
            "source_sha": SHA, "request_id": REQUEST_ID, **tools,
        })

    def test_pr_number_is_forwarded_and_added_to_private_run_title(self):
        self.assertEqual(self.request(pr_number="1190"), 42)
        self.assertEqual(self.github.writes[0][2]["inputs"], {
            "source_sha": SHA, "request_id": REQUEST_ID, "pr_number": "1190",
        })
        summary = Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
        self.assertIn("for PR #1190", summary)

    def test_private_dependency_manifest_is_requested_and_required(self):
        with self.assertRaisesRegex(RuntimeError, "private-image-lock"):
            self.request(lock_published_images=True)
        self.assertEqual(self.github.writes[0][2]["inputs"]["lock_published_images"], "true")
        self.github.artifacts.append({"name":f"private-image-lock-{REQUEST_ID}", "expired":False})
        self.assertEqual(self.request(lock_published_images=True), 42)

    def test_test_archive_is_required_even_when_all_images_succeed(self):
        for archive in (None, {"name": f"cloud-test-binaries-{SHA}", "expired": True},
                        {"name": f'cloud-test-binaries-{"c" * 40}', "expired": False}):
            with self.subTest(archive=archive):
                self.github.artifacts = [{"name": name, "expired": False} for name in build.ARTIFACTS]
                if archive:
                    self.github.artifacts.append(archive)
                with self.assertRaisesRegex(RuntimeError, "cloud-test-binaries"):
                    self.request(ci_tools_sha="b" * 40, test_features="ci-test",
                                 test_rust_toolchain="1.98.0")
                self.assertFalse(self.output.exists())

    def test_invalid_test_build_settings_cannot_dispatch(self):
        valid = {"ci_tools_sha": "b" * 40, "test_features": "ci-test",
                 "test_rust_toolchain": "1.98.0"}
        for key, value in (("ci_tools_sha", "main"), ("test_features", ""),
                           ("test_features", "ci-test\nother"), ("test_rust_toolchain", "stable")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.request(**{**valid, key: value})
        self.assertFalse(self.github.calls)

    def test_reuses_existing_correlated_build_without_dispatching_twice(self):
        self.github.request("POST", "example/dispatches")
        self.github.calls.clear()
        self.assertEqual(self.request(), 42)
        self.assertFalse(self.github.writes)

    def test_long_wait_reports_progress_without_dispatching_another_build(self):
        self.github.conclusion = None

        def wait(seconds):
            self.github.wait(seconds)
            if self.github.elapsed >= 90:
                self.github.conclusion = "success"

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.request(wait=wait), 42)
        log = output.getvalue()
        self.assertIn("Private image build 42: queued", log)
        self.assertGreaterEqual(log.count("Private image build 42: in_progress"), 2)
        self.assertIn("Private image build 42: completed", log)
        self.assertEqual(len(self.github.writes), 1)

    def test_failed_build_does_not_start_tests_or_cancel_a_completed_run(self):
        self.github.conclusion = "failure"
        with self.assertRaisesRegex(RuntimeError, "failure"):
            self.request()
        self.assertFalse(self.output.exists())
        self.assertFalse(any(path.endswith("/cancel") for _, path, _ in self.github.calls))

    def test_missing_or_expired_image_fails_before_starting_test_matrix(self):
        for artifacts in (self.github.artifacts[:-1],
                          [{**item, "expired": True} for item in self.github.artifacts]):
            with self.subTest(artifacts=artifacts):
                self.github.artifacts = artifacts
                with self.assertRaisesRegex(RuntimeError, "missing artifacts"):
                    self.request()
                self.assertFalse(self.output.exists())

    def test_timeout_cancels_only_the_correlated_run(self):
        self.github.conclusion = None
        with self.assertRaises(TimeoutError):
            self.request(timeout=1)
        self.assertEqual(self.github.writes[-1][:2], ("POST", f"{build.API_ROOT}/runs/42/cancel"))
        self.assertEqual(len(self.github.writes), 2)

    def test_caller_cancellation_cancels_its_private_build(self):
        def interrupted(_seconds):
            raise InterruptedError("cancelled")
        with self.assertRaises(InterruptedError):
            self.request(wait=interrupted)
        self.assertEqual(self.github.writes[-1][:2], ("POST", f"{build.API_ROOT}/runs/42/cancel"))

    def test_invalid_refs_cannot_dispatch_a_build(self):
        for sha, request_id, pr_number in (
            ("main", REQUEST_ID, None), (SHA, "untrusted\ninput", None),
            (SHA, REQUEST_ID, "0"), (SHA, REQUEST_ID, "1190\nother"),
        ):
            with self.subTest(sha=sha, request_id=request_id, pr_number=pr_number), \
                    self.assertRaises(ValueError):
                build.request_build(sha, request_id, pr_number=pr_number,
                                    request=self.github.request)
        self.assertFalse(self.github.calls)

    def fail_observations(self, errors):
        original = self.github.request
        pending = iter(errors)

        def request(method, path, payload=None):
            if method == "GET" and path.endswith("/runs/42"):
                error = next(pending, None)
                if error is not None:
                    self.github.calls.append((method, path, payload))
                    raise error
            return original(method, path, payload)
        self.github.request = request

    def test_primary_limit_waits_until_reset_without_cancel_or_redispatch(self):
        self.fail_observations([build.GitHubAPIError("GET", "run", status=403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1300",
        }, message="API rate limit exceeded")])
        self.assertEqual(self.request(wall_time=lambda: 1000 + self.github.elapsed), 42)
        self.assertEqual(self.github.elapsed, 301)
        self.assertEqual(len(self.github.writes), 1)

    def test_secondary_limit_and_429_honor_retry_after_without_cancel(self):
        for status in (403, 429):
            with self.subTest(status=status):
                self.github = FakeGitHub()
                self.fail_observations([build.GitHubAPIError("GET", "run", status=status,
                    headers={"retry-after": "75"}, message="secondary rate limit")])
                self.assertEqual(self.request(), 42)
                self.assertEqual(self.github.elapsed, 90)
                self.assertEqual(len(self.github.writes), 1)

    def test_secondary_limit_without_hint_uses_bounded_exponential_wait(self):
        self.fail_observations([build.GitHubAPIError("GET", "run", status=429)] * 2)
        self.assertEqual(self.request(), 42)
        self.assertEqual(self.github.elapsed, 195)
        self.assertEqual(len(self.github.writes), 1)

    def test_network_and_server_failures_retry_reads_only(self):
        self.fail_observations([
            build.GitHubAPIError("GET", "run", transport=True),
            build.GitHubAPIError("GET", "run", status=503),
        ])
        self.assertEqual(self.request(), 42)
        self.assertEqual(self.github.elapsed, 21)
        self.assertEqual(len(self.github.writes), 1)

    def test_permanent_auth_and_missing_private_run_fail_without_retry_or_cancel(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.github = FakeGitHub()
                self.fail_observations([build.GitHubAPIError("GET", "run", status=status,
                    message="Resource not accessible")])
                with self.assertRaises(build.GitHubAPIError):
                    self.request()
                self.assertEqual(self.github.elapsed, 15)
                self.assertEqual(len(self.github.writes), 1)
                self.assertFalse(self.output.exists())

    def test_exhausted_observation_retries_preserve_active_matched_build_for_reuse(self):
        self.fail_observations([build.GitHubAPIError("GET", "run", status=503)] * 4)
        with self.assertRaises(build.ObservationUnavailable):
            self.request()
        self.assertEqual(self.github.elapsed, 29)
        self.assertEqual(len(self.github.writes), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.request(), 42)
        self.assertEqual(len(self.github.writes), 1, "reuse must not redispatch or cancel")

    def test_rate_reset_beyond_deadline_does_not_poll_early_or_cancel(self):
        self.fail_observations([build.GitHubAPIError("GET", "run", status=403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1600",
        })])
        with self.assertRaises(build.ObservationUnavailable):
            self.request(timeout=90, wall_time=lambda: 1000 + self.github.elapsed)
        self.assertEqual(self.github.elapsed, 90)
        polls = [path for method, path, _ in self.github.calls
                 if method == "GET" and path.endswith("/runs/42")]
        self.assertEqual(len(polls), 1)
        self.assertEqual(len(self.github.writes), 1)
        self.assertFalse(self.output.exists())

    def test_ambiguous_dispatch_leaves_correlated_build_and_is_never_repeated(self):
        original = self.github.request
        def request(method, path, payload=None):
            result = original(method, path, payload)
            if path.endswith("/dispatches"):
                raise build.GitHubAPIError(method, path, transport=True)
            return result
        self.github.request = request
        with self.assertRaises(build.GitHubAPIError):
            self.request()
        self.assertEqual(len(self.github.writes), 1)
        self.assertEqual(self.request(), 42)
        self.assertEqual(len(self.github.writes), 1)

    def test_unclassified_observation_failure_does_not_cancel_an_active_build(self):
        self.fail_observations([RuntimeError("cannot observe private run")])
        with self.assertRaisesRegex(RuntimeError, "cannot observe"):
            self.request()
        self.assertEqual(len(self.github.writes), 1, "read failure cannot authorize cancellation")
        self.assertFalse(self.output.exists())

    def test_changed_poll_identity_cancels_only_original_correlated_run(self):
        original = self.github.request
        def request(method, path, payload=None):
            result = original(method, path, payload)
            if path.endswith("/runs/42"):
                result = {**result, "id": 999, "display_title": "unrelated"}
            return result
        self.github.request = request
        with self.assertRaises(build.BuildProvenanceError):
            self.request()
        self.assertEqual(self.github.writes[-1][:2], ("POST", f"{build.API_ROOT}/runs/42/cancel"))
        self.assertFalse(self.output.exists())

    def test_transient_artifact_read_preserves_exact_source_artifact_validation(self):
        original = self.github.request
        pending = True
        def request(method, path, payload=None):
            nonlocal pending
            if "/artifacts?" in path and pending:
                pending = False
                self.github.calls.append((method, path, payload))
                raise build.GitHubAPIError(method, path, status=502)
            return original(method, path, payload)
        self.github.request = request
        self.assertEqual(self.request(), 42)
        self.assertEqual(len(self.github.writes), 1)

    def test_ambiguous_dispatch_is_not_retried(self):
        with patch.object(build.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("gh", 30)) as run:
            with self.assertRaises(RuntimeError):
                build.api("POST", "example/dispatches", {"ref": "main"})
            self.assertEqual(run.call_count, 1)


class ApiTests(unittest.TestCase):
    def response(self, status, body, headers=None, stderr=""):
        rendered = "\r\n".join(f"{k}: {v}" for k, v in (headers or {}).items())
        return subprocess.CompletedProcess([], 0 if status < 400 else 1,
            f"HTTP/2.0 {status} Response\r\n{rendered}\r\n\r\n" + body, stderr)

    def test_error_preserves_only_safe_bounded_evidence(self):
        response = self.response(403, '{"message":"secondary rate limit secret-token"}', {
            "Retry-After": "65", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1234",
            "X-GitHub-Request-Id": "trace-123", "Authorization": "Bearer secret-token",
            "Set-Cookie": "private-cookie",
        })
        with patch.dict(os.environ, {"GH_TOKEN": "secret-token"}), \
                patch.object(build.subprocess, "run", return_value=response) as run:
            with self.assertRaises(build.GitHubAPIError) as failure:
                build.api_once("GET", "example/run")
        error = failure.exception
        self.assertEqual(error.status, 403)
        self.assertTrue(error.rate_limited)
        self.assertEqual(error.headers["x-github-request-id"], "trace-123")
        self.assertIn("[redacted]", str(error))
        self.assertNotIn("secret-token", str(error))
        self.assertNotIn("private-cookie", str(error))
        self.assertNotIn("authorization", error.headers)
        self.assertEqual(run.call_count, 1)

    def test_success_and_empty_mutation_response_keep_existing_json_contract(self):
        for status, body, expected in ((200, '{"id":42}', {"id": 42}), (204, "", None)):
            with self.subTest(status=status), patch.object(build.subprocess, "run",
                    return_value=self.response(status, body)):
                self.assertEqual(build.api_once("GET" if status == 200 else "POST", "example"), expected)

    def test_non_http_network_failure_is_retryable_but_cli_auth_failure_is_not(self):
        for stderr, transient in (("dial tcp: connection reset by peer", True),
                                  ("gh auth login required", False)):
            with self.subTest(stderr=stderr), patch.object(build.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 1, "", stderr)):
                with self.assertRaises(build.GitHubAPIError) as failure:
                    build.api_once("GET", "example")
                self.assertEqual(failure.exception.retryable, transient)

    def test_missing_status_or_malformed_json_is_not_silently_retried(self):
        for response in (subprocess.CompletedProcess([], 0, '{"id":42}', ""),
                         self.response(200, 'not-json')):
            with self.subTest(response=response), patch.object(build.subprocess, "run", return_value=response):
                with self.assertRaises(build.GitHubAPIError) as failure:
                    build.api_once("GET", "example")
                self.assertFalse(failure.exception.retryable)

    def test_process_timeout_uses_remaining_request_budget(self):
        elapsed = [0]
        def run(_command, **kwargs):
            self.assertEqual(kwargs["timeout"], 1.5)
            elapsed[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired("gh", kwargs["timeout"])
        with patch.object(build.subprocess, "run", side_effect=run) as process:
            with self.assertRaises(build.ObservationUnavailable):
                build.read_with_retry(None, "example", 1.5, lambda delay: elapsed.__setitem__(0, elapsed[0]+delay),
                                      lambda: elapsed[0], lambda: 1000)
            self.assertEqual(process.call_count, 1)
        self.assertEqual(elapsed[0], 1.5)

    def test_positive_read_completing_after_deadline_cannot_start_tests(self):
        elapsed = [0]
        def request(*_args):
            elapsed[0] = 2
            return {"status": "completed", "conclusion": "success"}
        with self.assertRaises(build.ObservationUnavailable):
            build.read_with_retry(request, "example", 1, lambda delay: None,
                                  lambda: elapsed[0], lambda: 1000)

    def test_known_transient_statuses_and_permanent_denials_are_distinguished(self):
        for status, expected in ((401, False), (403, False), (404, False), (429, True),
                                 (500, True), (502, True), (503, True), (504, True)):
            self.assertEqual(build.GitHubAPIError("GET", "example", status=status).retryable, expected)

    def test_artifact_reader_retains_three_bounded_transient_attempts(self):
        elapsed = [0]
        def wait(delay):
            elapsed[0] += delay
        with patch.object(build, "api_once", side_effect=build.GitHubAPIError("GET", "x", status=503)) as call, \
                patch.object(build.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(build.time, "sleep", side_effect=wait):
            with self.assertRaises(build.ObservationUnavailable):
                build.api("GET", "example")
        self.assertEqual(call.call_count, 3)
        self.assertEqual(elapsed[0], 6)

    def test_both_rate_headers_require_the_later_deadline(self):
        error = build.GitHubAPIError("GET", "example", status=429, headers={
            "retry-after": "10", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1200",
        })
        self.assertEqual(error.retry_delay(0, 1000), 201)


if __name__ == "__main__":
    unittest.main()
