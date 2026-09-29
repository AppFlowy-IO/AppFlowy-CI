"""Test the private build handoff without dispatching GitHub workflows."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import request_image_build as build


SHA = "a" * 40
REQUEST_ID = "1234-2"


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.run = None
        self.conclusion = "success"
        self.artifacts = [{"name": name, "expired": False} for name in build.ARTIFACTS]
        self.elapsed = 0

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path.endswith("/dispatches"):
            self.run = {
                "id": 42, "status": "queued", "conclusion": None,
                "display_title": f"CI images {REQUEST_ID} ({SHA})",
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
        self.assertEqual(self.output.read_text(), "run_id=42\n")
        self.assertEqual(len(self.github.writes), 1)
        self.assertEqual(self.github.writes[0][2], {
            "ref": "main", "inputs": {"source_sha": SHA, "request_id": REQUEST_ID},
        })

    def test_reuses_existing_correlated_build_without_dispatching_twice(self):
        self.github.request("POST", "example/dispatches")
        self.github.calls.clear()
        self.assertEqual(self.request(), 42)
        self.assertFalse(self.github.writes)

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
        for sha, request_id in (("main", REQUEST_ID), (SHA, "untrusted\ninput")):
            with self.subTest(sha=sha, request_id=request_id), self.assertRaises(ValueError):
                build.request_build(sha, request_id, request=self.github.request)
        self.assertFalse(self.github.calls)

    def test_ambiguous_dispatch_is_not_retried(self):
        with patch.object(build.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("gh", 30)) as run:
            with self.assertRaises(RuntimeError):
                build.api("POST", "example/dispatches", {"ref": "main"})
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
