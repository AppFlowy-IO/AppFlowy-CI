"""Exercise PR and parent-attempt cancellation without contacting GitHub."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import yaml

import ci_run_lifecycle as lifecycle


CLIENT_REPO = "AppFlowy-IO/AppFlowy-Premium"
# Include the workflows that received metadata in #42, even though future runs
# no longer carry it. Already queued/running titles must also be ignored.
UNRELATED_WORKFLOWS = {
    CLIENT_REPO: (
        "flutter_ci.yaml", "ios_ci.yaml", "rust_ci.yaml", "mobile_ci.yml",
        "rust_coverage.yml", "webhook_receiver.yaml",
    ),
    lifecycle.CI_REPO: (
        "flutter_ci.yaml", "ios_ci.yaml", "rust_ci.yaml", "workflow_checks.yaml",
    ),
    lifecycle.CLOUD_REPO: (
        "cloud_backend_ci.yaml", "cloud_commercial_integration_ci.yaml",
        "cloud_docker_ci.yaml", "cloud_e2e_ci.yaml", "cloud_frontend_ci.yaml",
        "cloud_rustlint_ci.yaml", "webhook_receiver.yaml",
    ),
}


def run(run_id, *, source=lifecycle.CLOUD_REPO, pr=123, parent=None,
        workflow="cloud_integration_ci.yaml", event="repository_dispatch", status="queued",
        attempt=1):
    return {
        "id": run_id, "run_attempt": attempt, "status": status, "event": event,
        "path": f".github/workflows/{workflow}",
        "display_title": (
            f"[ci source={source} pr={pr or 'none'} "
            f"parent={f'{parent[0]}-{parent[1]}' if parent else 'none'}] Example"
        ),
    }


def image(run_id, parent, *, attempt=1, pr_number=None, **kwargs):
    result = run(
        run_id, workflow="build_ci_images_self_hosted.yml", event="workflow_dispatch", **kwargs,
    )
    suffix = f" [PR #{pr_number}]" if pr_number else ""
    result["display_title"] = f"CI images {parent}-{attempt} ({'a' * 40}){suffix}"
    return result


class FakeGitHub:
    def __init__(self):
        self.runs = {lifecycle.CI_REPO: [], lifecycle.CLOUD_REPO: []}
        self.prs = {}
        self.parents = {}
        self.calls = []
        self.failed_cancels = {}
        self.current = {}

    def request(self, method, path):
        self.calls.append((method, path))
        route, _, query = path.partition("?")
        parts = route.split("/")
        repo = "/".join(parts[1:3])
        if method == "POST":
            run_id = int(parts[-2])
            if run_id in self.failed_cancels:
                raise lifecycle.ApiError("cancellation failed")
            return None
        if parts[3] == "pulls":
            return {"state": self.prs[(repo, int(parts[4]))]}
        if "/attempts/" in path:
            return copy.deepcopy(self.parents[(int(parts[5]), int(parts[7]))])
        if not query:
            run_id = int(parts[-1])
            if run_id in self.current:
                return self.current[run_id]
            if (run_id in self.failed_cancels and
                    ("POST", path + "/cancel") in self.calls):
                return {"status": self.failed_cancels[run_id]}
            return next(r for r in self.runs[repo] if r["id"] == run_id)
        params = parse_qs(query)
        runs = self.runs[repo]
        if "status" in params:
            runs = [r for r in runs if r["status"] == params["status"][0]]
        page = int(params.get("page", ["1"])[0])
        return {"workflow_runs": copy.deepcopy(runs[(page - 1) * 100:page * 100])}

    @property
    def cancellations(self):
        return [path for method, path in self.calls if method == "POST"]


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.api.prs[(lifecycle.CLOUD_REPO, 123)] = "closed"

    def cleanup(self, apply=True):
        with redirect_stdout(io.StringIO()):
            return lifecycle.cleanup(self.api.request, apply=apply)

    def test_closed_pr_cancels_running_and_queued_jobs_and_laptop_build(self):
        self.api.runs[lifecycle.CI_REPO] = [
            run(1), run(2, status="in_progress"), run(3, status="pending"),
        ]
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1, pr_number=123)]
        self.assertEqual(self.cleanup(), [
            (lifecycle.CI_REPO, 1), (lifecycle.CI_REPO, 2), (lifecycle.CI_REPO, 3),
            (lifecycle.CLOUD_REPO, 10),
        ])

    def test_client_source_cannot_claim_cloud_integration_workflow(self):
        self.api.prs[(CLIENT_REPO, 123)] = "closed"
        self.api.runs[lifecycle.CI_REPO] = [
            run(1),
            run(2, source=CLIENT_REPO),
        ]
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1)])
        self.assertFalse(any(CLIENT_REPO in path for _, path in self.api.calls))

    def test_unrelated_old_metadata_survives_closed_pr_and_cancelled_parent(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "cancelled"}
        for source, workflows in UNRELATED_WORKFLOWS.items():
            for workflow in workflows:
                with self.subTest(source=source, workflow=workflow):
                    self.api.calls.clear()
                    self.api.prs[(source, 123)] = "closed"
                    self.api.runs[lifecycle.CI_REPO] = [
                        run(1, source=source, workflow=workflow, parent=(9, 1)),
                    ]
                    self.assertEqual(self.cleanup(), [])
                    self.assertFalse(any(
                        "/pulls/" in path or "/attempts/" in path
                        for _, path in self.api.calls
                    ))

    def test_main_push_manual_build_release_and_unknown_legacy_title_survive(self):
        legacy = run(4)
        legacy["display_title"] = "cloud-premium-integration-ci"
        self.api.runs[lifecycle.CI_REPO] = [
            run(1, pr=None), run(2, pr=None, event="workflow_dispatch"), legacy,
        ]
        self.api.runs[lifecycle.CLOUD_REPO] = [
            run(3, workflow="build_docker_self_hosted.yml", event="workflow_dispatch"),
        ]
        self.assertEqual(self.cleanup(), [])

    def test_reopened_pr_is_not_cancelled(self):
        self.api.prs[(lifecycle.CLOUD_REPO, 123)] = "open"
        self.api.runs[lifecycle.CI_REPO] = [run(1)]
        self.assertEqual(self.cleanup(), [])

    def test_completed_runs_are_not_cancelled(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1, status="completed")]
        self.assertEqual(self.cleanup(), [])

    def test_cancellation_of_parent_cascades_to_child_and_image(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "cancelled"}
        self.api.runs[lifecycle.CI_REPO] = [run(1, pr=None, parent=(9, 1))]
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1)]
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1), (lifecycle.CLOUD_REPO, 10)])

    def test_cancelled_attempt_does_not_cancel_retry(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "cancelled"}
        self.api.parents[(9, 2)] = {
            **run(9, attempt=2, status="in_progress"), "conclusion": None,
        }
        self.api.runs[lifecycle.CI_REPO] = [
            run(1, pr=None, parent=(9, 1)), run(2, pr=None, parent=(9, 2)),
        ]
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 9, attempt=2)]
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1)])

    def test_completed_dispatcher_does_not_cancel_its_children(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "success"}
        self.api.runs[lifecycle.CI_REPO] = [run(1, pr=None, parent=(9, 1))]
        self.assertEqual(self.cleanup(), [])

    def test_late_private_dispatch_is_found_by_next_cleanup(self):
        self.api.parents[(9, 1)] = {
            **run(9, status="completed"), "conclusion": "cancelled",
            "display_title": "cloud-premium-integration-ci",
        }
        self.assertEqual(self.cleanup(), [])
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 9)]
        self.assertEqual(self.cleanup(), [(lifecycle.CLOUD_REPO, 10)])

    def test_private_image_request_must_belong_to_cloud_integration(self):
        self.api.parents[(9, 1)] = {
            **run(9, source=CLIENT_REPO, workflow="flutter_ci.yaml", status="completed"),
            "conclusion": "cancelled",
        }
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 9)]
        self.assertEqual(self.cleanup(), [])

    def test_all_active_statuses_and_pages_are_visited(self):
        self.api.runs[lifecycle.CI_REPO] = [run(n) for n in range(1, 102)]
        self.api.runs[lifecycle.CI_REPO] += [
            run(200, status="waiting"), run(201, status="requested"),
        ]
        self.assertEqual(len(self.cleanup()), 103)
        self.assertEqual(sum("/pulls/" in p for _, p in self.api.calls), 1)

    def test_dry_run_identifies_complete_chain_without_writes(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1)]
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1)]
        self.assertEqual(len(self.cleanup(apply=False)), 2)
        self.assertEqual(self.api.cancellations, [])

    def test_finish_during_cancel_is_harmless(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1)]
        self.api.failed_cancels[1] = "completed"
        self.assertEqual(self.cleanup(), [])

    def test_retry_started_after_snapshot_is_not_cancelled(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "cancelled"}
        self.api.runs[lifecycle.CI_REPO] = [run(1, pr=None, parent=(9, 1))]
        self.api.current[1] = {"status": "in_progress", "run_attempt": 2}
        self.assertEqual(self.cleanup(), [])
        self.assertEqual(self.api.cancellations, [])

    def test_completion_before_cancel_is_not_written(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1)]
        self.api.current[1] = {"status": "completed", "run_attempt": 1}
        self.assertEqual(self.cleanup(), [])
        self.assertEqual(self.api.cancellations, [])

    def test_one_failed_cancellation_does_not_skip_other_runs(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1), run(2)]
        self.api.failed_cancels[1] = "in_progress"
        with self.assertRaisesRegex(RuntimeError, "cancellation failed"):
            self.cleanup()
        self.assertEqual(len(self.api.cancellations), 2)

    def test_native_pr_cannot_claim_private_source_identity(self):
        self.api.runs[lifecycle.CI_REPO] = [
            run(1, event="pull_request"),
        ]
        self.assertEqual(self.cleanup(), [])

    def test_native_ci_pr_is_outside_cloud_cleanup(self):
        self.api.prs[(lifecycle.CI_REPO, 123)] = "closed"
        self.api.runs[lifecycle.CI_REPO] = [
            run(1, source=lifecycle.CI_REPO, event="pull_request", workflow="flutter_ci.yaml"),
            run(2, source=lifecycle.CI_REPO, event="pull_request", workflow="workflow_checks.yaml"),
        ]
        self.assertEqual(self.cleanup(), [])
        self.assertFalse(any("/pulls/" in path for _, path in self.api.calls))

    def test_rejects_unexpected_source_and_invalid_pr_before_request(self):
        for source, number in (
            (CLIENT_REPO, 123), (lifecycle.CI_REPO, 123), ("evil/repo", 123),
            (lifecycle.CLOUD_REPO, "123/../../runs"),
        ):
            with self.assertRaises(ValueError):
                lifecycle.pr_is_open(source, number, self.api.request)
        self.assertEqual(self.api.calls, [])


class LegacyDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.api.parents[(9, 2)] = {"run_started_at": "2026-09-29T00:00:00Z"}

    def test_finds_exact_parent_attempt_among_concurrent_dispatches(self):
        self.api.runs[lifecycle.CI_REPO] = [
            run(1, parent=(9, 1)), run(2, parent=(10, 2)), run(3, parent=(9, 2)),
        ]
        self.assertEqual(
            lifecycle.find_children(["integration"], (9, 2), self.api.request),
            {"integration": 3},
        )

    def test_waits_for_delayed_children(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.api.runs[lifecycle.CI_REPO] = [run(3, parent=(9, 2))]

        self.assertEqual(
            lifecycle.find_children(["integration"], (9, 2), self.api.request, wait=wait),
            {"integration": 3},
        )
        self.assertEqual(waits, [5])

    def test_duplicate_dispatch_fails_instead_of_monitoring_wrong_run(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1, parent=(9, 2)), run(2, parent=(9, 2))]
        with self.assertRaisesRegex(RuntimeError, "Multiple integration"):
            lifecycle.find_children(["integration"], (9, 2), self.api.request)

    def test_old_dispatcher_finds_restored_workflows_without_metadata(self):
        restored = run(1, workflow="cloud_rustlint_ci.yaml")
        restored["display_title"] = "cloud-premium-rustlint-ci"
        self.api.runs[lifecycle.CI_REPO] = [
            restored, run(2, parent=(9, 2)), run(3, parent=(10, 2)),
        ]
        self.assertEqual(
            lifecycle.find_children(["rustlint", "integration"], (9, 2), self.api.request),
            {"rustlint": 1, "integration": 2},
        )
        self.assertEqual(self.api.cancellations, [])

    def test_old_client_dispatcher_prefers_exact_metadata_when_available(self):
        restored = run(1, source=CLIENT_REPO, workflow="flutter_ci.yaml")
        restored["display_title"] = "private-repo-flutter-ci"
        self.api.runs[lifecycle.CI_REPO] = [
            restored, run(2, source=CLIENT_REPO, workflow="flutter_ci.yaml", parent=(9, 2)),
        ]
        self.assertEqual(
            lifecycle.find_children(["flutter"], (9, 2), self.api.request),
            {"flutter": 2},
        )
        self.assertEqual(self.api.cancellations, [])

    def test_old_pr_gate_does_not_read_client_prs_or_cancel_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            with patch.dict("os.environ", {"GITHUB_OUTPUT": str(output)}, clear=True):
                with patch("sys.argv", ["ci_run_lifecycle.py", "--check-pr"]):
                    with patch.object(lifecycle, "api") as api:
                        with patch.object(lifecycle, "cleanup") as cleanup:
                            lifecycle.main()
                        cleanup.assert_not_called()
                    api.assert_not_called()
            self.assertEqual(output.read_text(), "active=true\n")


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(__file__).resolve().parents[1] / "workflows"
        cls.workflows = {
            path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            for path in directory.iterdir()
        }

    def test_only_cloud_integration_exposes_lifecycle_identity(self):
        title = self.workflows[lifecycle.INTEGRATION_WORKFLOW]["run-name"]
        self.assertIn("[ci source=", title)
        self.assertIn("pr=", title)
        self.assertIn("parent_run_attempt", title)
        for workflows in UNRELATED_WORKFLOWS.values():
            for name in workflows:
                with self.subTest(workflow=name):
                    self.assertNotIn("[ci source=", self.workflows[name].get("run-name", ""))

    def test_only_integration_dispatch_forwards_the_exact_parent(self):
        job = self.workflows["webhook_receiver.yaml"]["jobs"]["dispatch-workflows"]
        for step in job["steps"]:
            if step.get("uses", "").startswith("peter-evans/repository-dispatch@"):
                with self.subTest(step=step["name"]):
                    payload = json.loads(step["with"]["client-payload"])
                    if step["with"]["event-type"] == "cloud-premium-integration-ci":
                        self.assertEqual(payload["parent_run_id"], "${{ github.run_id }}")
                        self.assertEqual(payload["parent_run_attempt"], "${{ github.run_attempt }}")
                    else:
                        self.assertNotIn("parent_run_id", payload)
                        self.assertNotIn("parent_run_attempt", payload)
                    self.assertNotIn("steps.lifecycle", step["if"])
        # New dispatchers no longer depend on the legacy metadata lookup.
        self.assertFalse(any("ci_run_lifecycle.py" in step.get("run", "") for step in job["steps"]))

    def test_privileged_cleanup_uses_only_default_branch_code(self):
        workflow = self.workflows["cancel_obsolete_ci.yaml"]
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertEqual(workflow["on"]["repository_dispatch"]["types"], ["ci-pr-closed"])
        self.assertEqual(workflow["on"]["workflow_run"]["types"], ["completed"])
        checkout = workflow["jobs"]["cancel"]["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")


if __name__ == "__main__":
    unittest.main()
