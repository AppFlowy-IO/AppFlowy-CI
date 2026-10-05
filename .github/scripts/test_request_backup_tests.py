"""Exercise private Backup dispatch/result/cleanup ownership without network access."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import request_backup_tests as backup


SHA = "a" * 40
REF = "feat/rust-server-backup-restore"
REQUEST = "1234-2"


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.run = None
        self.conclusion = "success"
        self.artifacts = [{"id": 456, "name": f"backup-integration-{REQUEST}", "expired": False}]
        self.elapsed = 0
        self.duplicate = False
        self.pull_repo = backup.REPOSITORY

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path.endswith("/dispatches"):
            self.run = {
                "id": 42, "status": "queued", "conclusion": None,
                "display_title": f"CI backup {REQUEST} ({SHA})",
            }
        elif "/pulls/" in path:
            return {"head": {"repo": {"full_name": self.pull_repo}, "ref": REF}}
        elif "/workflows/" in path and "/runs?" in path:
            unrelated = {"id": 999, "display_title": f"CI backup 9999-1 ({SHA})"}
            ours = [dict(self.run)] if self.run else []
            if self.duplicate:
                ours *= 2
            return {"workflow_runs": [unrelated, *ours]}
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


class BackupRequestTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "output"
        self.summary = Path(self.directory.name) / "summary"
        env = patch.dict(os.environ, {
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        })
        env.start()
        self.addCleanup(env.stop)
        self.github = FakeGitHub()

    def request(self, **kwargs):
        return backup.request_tests(
            SHA, REQUEST, kwargs.pop("source_ref", REF), request=self.github.request,
            now=lambda: self.github.elapsed, wait=kwargs.pop("wait", self.github.wait), **kwargs,
        )

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def test_dispatches_only_exact_requested_source_and_ci_suite(self):
        self.assertEqual(self.request(), 42)
        self.assertEqual(self.github.writes, [("POST", (
            f"{backup.API_ROOT}/workflows/backup-release-tests.yml/dispatches"
        ), {"ref": REF, "inputs": {
            "suite": "ci", "source_sha": SHA, "request_id": REQUEST,
        }})])
        outputs = self.outputs()
        self.assertEqual(outputs["run_id"], "42")
        self.assertEqual(outputs["artifact_id"], "456")
        self.assertEqual(outputs["artifact_url"], f"{outputs['run_url']}/artifacts/456")
        self.assertIn(SHA, self.summary.read_text())
        self.assertFalse(any("/artifacts/456/zip" in path for _, path, _ in self.github.calls))

    def test_reuses_exact_correlated_run_without_second_dispatch(self):
        self.github.request("POST", "fixture/dispatches")
        self.github.calls.clear()
        self.assertEqual(self.request(), 42)
        self.assertFalse(self.github.writes)

    def test_ambiguous_correlated_runs_fail_without_guessing(self):
        self.github.request("POST", "fixture/dispatches")
        self.github.calls.clear()
        self.github.duplicate = True
        with self.assertRaisesRegex(RuntimeError, "Multiple Backup runs"):
            self.request()
        self.assertFalse(self.github.writes)

    def test_waits_until_completed_even_after_long_running_restore(self):
        self.github.conclusion = None

        def wait(seconds):
            self.github.wait(seconds)
            if self.github.elapsed >= 90:
                self.github.conclusion = "success"

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.request(wait=wait), 42)
        self.assertIn("queued", output.getvalue())
        self.assertIn("in_progress", output.getvalue())
        self.assertIn("completed", output.getvalue())
        self.assertEqual(len(self.github.writes), 1)

    def test_failed_cancelled_or_skipped_private_run_is_never_success(self):
        for conclusion in ("failure", "cancelled", "skipped", "timed_out"):
            with self.subTest(conclusion=conclusion):
                self.github = FakeGitHub()
                self.github.conclusion = conclusion
                with self.assertRaisesRegex(RuntimeError, conclusion):
                    self.request()
                self.assertEqual(len(self.github.writes), 1)
                self.assertEqual(self.outputs()["artifact_id"], "456")
                self.assertEqual(self.outputs()["conclusion"], conclusion)
                self.output.unlink()

    def test_evidence_must_exist_be_unexpired_and_match_request(self):
        for artifacts in ([], [{"id": 456, "name": "backup-integration-other", "expired": False}],
                          [{"id": 456, "name": f"backup-integration-{REQUEST}", "expired": True}],
                          self.github.artifacts * 2):
            with self.subTest(artifacts=artifacts):
                self.github = FakeGitHub()
                self.github.artifacts = artifacts
                with self.assertRaisesRegex(RuntimeError, "exactly one evidence artifact"):
                    self.request()
                self.assertNotIn("artifact_id", self.outputs())
                self.output.unlink()

    def test_timeout_keeps_private_cleanup_running(self):
        self.github.conclusion = None
        with self.assertRaises(TimeoutError):
            self.request(timeout=1)
        self.assertEqual(len(self.github.writes), 1)
        self.assertFalse(any(path.endswith("/cancel") for _, path, _ in self.github.calls))
        self.assertEqual(self.outputs()["run_id"], "42")

    def test_caller_cancellation_keeps_private_cleanup_running(self):
        self.github.conclusion = None

        def interrupted(_seconds):
            raise InterruptedError("cancelled")

        with self.assertRaises(InterruptedError):
            self.request(wait=interrupted)
        self.assertEqual(len(self.github.writes), 1)
        self.assertFalse(any(path.endswith("/cancel") for _, path, _ in self.github.calls))

    def test_branch_tag_and_commit_select_dispatchable_workflow_ref(self):
        for source_ref, expected in ((REF, REF), (f"refs/heads/{REF}", REF),
                                     ("refs/tags/v1.0", "v1.0"), (SHA, "main"),
                                     ("refs/pull/1190/head", REF)):
            with self.subTest(ref=source_ref):
                self.github = FakeGitHub()
                self.assertEqual(self.request(source_ref=source_ref), 42)
                self.assertEqual(self.github.writes[0][2]["ref"], expected)
                self.assertEqual(self.github.writes[0][2]["inputs"]["source_sha"], SHA)
                self.output.unlink()

    def test_raw_sha_with_unsupported_main_workflow_fails_without_retry_or_cancellation(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "POST":
                self.assertEqual(payload["ref"], "main")
                self.assertEqual(payload["inputs"]["source_sha"], SHA)
                raise RuntimeError("Confirm the Premium workflow is merged")
            return self.github.request(method, path, payload)

        with self.assertRaisesRegex(RuntimeError, "workflow is merged"):
            backup.request_tests(SHA, REQUEST, SHA, request=request)
        self.assertEqual(sum(method == "POST" for method, _, _ in calls), 1)
        self.assertFalse(any(path.endswith("/cancel") for _, path, _ in calls))
        self.assertFalse(self.output.exists())

    def test_fork_workflow_definition_is_rejected(self):
        self.github.pull_repo = "someone/Cloud-fork"
        with self.assertRaisesRegex(ValueError, "private Cloud repository"):
            self.request(source_ref="refs/pull/1190/head")
        self.assertFalse(self.github.writes)

    def test_invalid_input_does_not_dispatch(self):
        for sha, request_id, ref in (("main", REQUEST, REF), (SHA, "1\n2", REF),
                                     (SHA, REQUEST, ""), (SHA, REQUEST, "main\nother"),
                                     (SHA, REQUEST, "refs/unsupported/one")):
            with self.subTest(sha=sha, request=request_id, ref=ref), self.assertRaises(ValueError):
                backup.request_tests(sha, request_id, ref, request=self.github.request)
        self.assertFalse(self.github.calls)


if __name__ == "__main__":
    unittest.main()
