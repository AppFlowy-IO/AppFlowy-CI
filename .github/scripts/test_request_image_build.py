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
        self.override_sha = SHA
        self.dispatched_sha = None

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path.endswith("/dispatches"):
            pr_number = (payload or {}).get("inputs", {}).get("pr_number")
            suffix = f" [PR #{pr_number}]" if pr_number else ""
            self.run = {
                "id": 42, "status": "queued", "conclusion": None,
                "display_title": f"CI images {REQUEST_ID} ({SHA}){suffix}",
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

    def test_explicit_gateway_build_is_forwarded_and_requires_companion(self):
        self.github.artifacts.append({"name": "companion-gateway-image", "expired": False})
        self.assertEqual(self.request(build_gateway=True), 42)
        self.assertEqual(self.github.writes[0][2]["inputs"]["build_gateway"], "true")

    def test_explicit_gateway_build_cannot_succeed_without_companion(self):
        with self.assertRaisesRegex(RuntimeError, "companion-gateway-image"):
            self.request(build_gateway=True)

    def test_gateway_source_selection_must_be_boolean(self):
        with self.assertRaises(ValueError):
            self.request(build_gateway="false")
        self.assertFalse(self.github.calls)

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
            self.assertIs(request.call_args.kwargs["build_gateway"], False)
            os.environ["BUILD_GATEWAY_FROM_SOURCE"] = "true"
            os.environ["IMAGE_WORKFLOW_REF"] = "fix/workspace-import-publication"
            build.main()
            self.assertEqual(request.call_args.kwargs["image_workflow_ref"],
                             "fix/workspace-import-publication")
            self.assertIs(request.call_args.kwargs["build_gateway"], True)

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

    def test_ambiguous_dispatch_is_not_retried(self):
        with patch.object(build.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("gh", 30)) as run:
            with self.assertRaises(RuntimeError):
                build.api("POST", "example/dispatches", {"ref": "main"})
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
