"""Exercise PR and parent-attempt cancellation without contacting GitHub."""

from contextlib import redirect_stdout
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import ci_run_lifecycle as lifecycle


CLIENT_REPO = "AppFlowy-IO/AppFlowy-Premium"
# These workflows received unversioned metadata in #42. Those old titles did
# not distinguish PR dispatches from pushes and must remain outside PR cleanup.
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
        attempt=1, legacy=False):
    return {
        "id": run_id, "run_attempt": attempt, "status": status, "event": event,
        "path": f".github/workflows/{workflow}",
        "display_title": (
            f"[{'ci' if legacy else 'ci-v2'} source={source} pr={pr or 'none'} "
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


def native_run(run_id, *, pr=90, workflow="workflow_checks.yaml", **kwargs):
    result = run(
        run_id, source=lifecycle.CI_REPO, pr=pr, workflow=workflow,
        event="pull_request", **kwargs,
    )
    result["display_title"] = "An ordinary PR title, unrelated to lifecycle metadata"
    result["pull_requests"] = [{
        "number": pr,
        "url": f"https://api.github.com/repos/{lifecycle.CI_REPO}/pulls/{pr}",
        "base": {"repo": {"url": f"https://api.github.com/repos/{lifecycle.CI_REPO}"}},
    }]
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
            value = self.prs[(repo, int(parts[4]))]
            return copy.deepcopy(value) if isinstance(value, dict) else {"state": value}
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
        self.api.prs[(lifecycle.CLOUD_REPO, 123)] = {"state": "closed", "merged": True}
        self.api.prs[(lifecycle.CI_REPO, 90)] = {
            "state": "closed", "merged": True, "merged_at": "2026-10-23T13:00:00Z",
        }

    def cleanup(self, apply=True):
        with redirect_stdout(io.StringIO()):
            return lifecycle.cleanup(self.api.request, apply=apply)

    def test_merged_pr_cancels_running_and_queued_jobs_and_laptop_build(self):
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
                        run(1, source=source, workflow=workflow, parent=(9, 1), legacy=True),
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

    def test_v2_allowlisted_source_workflows_are_cancelled_only_after_merge(self):
        for source, workflows in lifecycle.SOURCE_WORKFLOWS.items():
            for workflow in workflows:
                with self.subTest(source=source, workflow=workflow):
                    self.api.calls.clear()
                    self.api.runs[lifecycle.CI_REPO] = [run(1, source=source, workflow=workflow)]
                    self.api.prs[(source, 123)] = {"state": "closed", "merged": False}
                    self.assertEqual(self.cleanup(), [])
                    self.assertEqual(self.api.cancellations, [])
                    self.api.prs[(source, 123)] = {"state": "closed", "merged": True}
                    self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1)])

    def test_v2_rejects_unlisted_sources_workflows_and_events(self):
        for source, workflow, event in (
            (lifecycle.CI_REPO, "workflow_checks.yaml", "repository_dispatch"),
            ("AppFlowy-IO/other", "flutter_ci.yaml", "repository_dispatch"),
            (CLIENT_REPO, "cloud_backend_ci.yaml", "repository_dispatch"),
            (lifecycle.CLOUD_REPO, "flutter_ci.yaml", "repository_dispatch"),
            (CLIENT_REPO, "flutter_ci.yaml", "push"),
            (lifecycle.CLOUD_REPO, "cloud_integration_ci.yaml", "release"),
            (lifecycle.CLOUD_REPO, "cloud_integration_ci.yaml", "pull_request_target"),
        ):
            with self.subTest(source=source, workflow=workflow, event=event):
                self.api.calls.clear()
                self.api.runs[lifecycle.CI_REPO] = [run(1, source=source, workflow=workflow, event=event)]
                self.assertEqual(self.cleanup(), [])
                self.assertFalse(any("/pulls/" in path for _, path in self.api.calls))

    def test_equal_pr_numbers_in_different_repositories_are_independent(self):
        sources = (lifecycle.CI_REPO, CLIENT_REPO, lifecycle.CLOUD_REPO)
        self.api.runs[lifecycle.CI_REPO] = [
            native_run(1, pr=123),
            run(2, source=CLIENT_REPO, workflow="flutter_ci.yaml"),
            run(3, workflow="cloud_backend_ci.yaml"),
        ]
        for run_id, merged_source in enumerate(sources, 1):
            with self.subTest(merged_source=merged_source):
                self.api.calls.clear()
                for source in sources:
                    merged = source == merged_source
                    self.api.prs[(source, 123)] = {
                        "state": "closed" if merged else "open", "merged": merged,
                    }
                self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, run_id)])

    def test_legacy_cloud_pr_number_cannot_authorize_pr_cleanup(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1, legacy=True)]
        self.assertEqual(self.cleanup(), [])
        self.assertFalse(any("/pulls/" in path for _, path in self.api.calls))

    def test_legacy_cloud_parent_cancellation_still_cascades_to_images(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "cancelled"}
        self.api.runs[lifecycle.CI_REPO] = [run(1, parent=(9, 1), legacy=True)]
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1)]
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1), (lifecycle.CLOUD_REPO, 10)])
        self.assertFalse(any("/pulls/" in path for _, path in self.api.calls))

    def test_other_v2_workflows_cannot_authorize_image_or_parent_cancellation(self):
        for source, workflow in (
            (CLIENT_REPO, "flutter_ci.yaml"), (lifecycle.CLOUD_REPO, "cloud_backend_ci.yaml"),
        ):
            with self.subTest(source=source, workflow=workflow):
                self.api.calls.clear()
                self.api.prs[(source, 123)] = {"state": "closed", "merged": True}
                candidate = run(1, source=source, workflow=workflow)
                self.api.runs[lifecycle.CI_REPO] = [candidate, run(2, pr=None, parent=(1, 1))]
                self.api.parents[(1, 1)] = candidate
                self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1)]
                self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1)])

    def test_completed_runs_are_not_cancelled(self):
        self.api.runs[lifecycle.CI_REPO] = [run(1, status="completed")]
        self.assertEqual(self.cleanup(), [])

    def test_cancellation_of_parent_cascades_to_child_and_image(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "cancelled"}
        self.api.runs[lifecycle.CI_REPO] = [run(1, pr=None, parent=(9, 1))]
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1)]
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1), (lifecycle.CLOUD_REPO, 10)])

    def test_backup_restore_survives_parent_cancellation_and_pr_close(self):
        self.api.parents[(9, 1)] = {"status": "completed", "conclusion": "cancelled"}
        self.api.runs[lifecycle.CI_REPO] = [run(1, parent=(9, 1))]
        backup = run(11, workflow="backup-release-tests.yml", event="workflow_dispatch",
                     status="in_progress")
        backup["display_title"] = f"CI backup 1-1 ({'a' * 40})"
        self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1), backup]
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1), (lifecycle.CLOUD_REPO, 10)])
        self.assertFalse(any("runs/11" in path for path in self.api.cancellations))

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

    def test_native_ci_pr_title_without_api_association_is_not_trusted(self):
        self.api.prs[(lifecycle.CI_REPO, 123)] = "closed"
        self.api.runs[lifecycle.CI_REPO] = [
            run(1, source=lifecycle.CI_REPO, event="pull_request", workflow="flutter_ci.yaml"),
            run(2, source=lifecycle.CI_REPO, event="pull_request", workflow="workflow_checks.yaml"),
        ]
        self.assertEqual(self.cleanup(), [])
        self.assertFalse(any("/pulls/" in path for _, path in self.api.calls))

    def test_merged_native_pr_cancels_all_its_workflows_using_api_associations(self):
        workflows = (
            "flutter_ci.yaml", "rust_ci.yaml", "ios_ci.yaml",
            "workflow_checks.yaml", "cloud_integration_ci.yaml",
        )
        self.api.runs[lifecycle.CI_REPO] = [
            native_run(index, workflow=workflow, status=status)
            for index, (workflow, status) in enumerate(zip(workflows, lifecycle.ACTIVE), 1)
        ]
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, index) for index in range(1, 6)])
        self.assertEqual(
            [path for _, path in self.api.calls if "/pulls/" in path],
            [f"repos/{lifecycle.CI_REPO}/pulls/90"],
        )

    def test_native_pr_requires_a_fresh_closed_and_merged_response(self):
        for response in (
            {"state": "open", "merged": False},
            {"state": "open", "merged": True},
            {"state": "closed", "merged": False, "merged_at": None},
            {"state": "closed"},
            {"state": "closed", "merged": "true", "merged_at": ""},
            {"merged": True},
        ):
            with self.subTest(response=response):
                self.api.calls.clear()
                self.api.prs[(lifecycle.CI_REPO, 90)] = response
                self.api.runs[lifecycle.CI_REPO] = [native_run(1)]
                self.assertEqual(self.cleanup(), [])
                self.assertEqual(self.api.cancellations, [])
                self.assertIn(("GET", f"repos/{lifecycle.CI_REPO}/pulls/90"), self.api.calls)

        for marker in ({"merged": True}, {"merged_at": "2026-10-23T13:00:00Z"}):
            with self.subTest(marker=marker):
                self.api.prs[(lifecycle.CI_REPO, 90)] = {"state": "closed", **marker}
                self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1)])

    def test_non_native_events_cannot_claim_a_merged_native_association(self):
        for event in ("push", "release", "workflow_dispatch", "repository_dispatch",
                      "pull_request_target", "schedule"):
            with self.subTest(event=event):
                self.api.calls.clear()
                candidate = native_run(1)
                candidate["event"] = event
                self.api.runs[lifecycle.CI_REPO] = [candidate]
                self.assertEqual(self.cleanup(), [])
                self.assertFalse(any("/pulls/" in path for _, path in self.api.calls))

    def test_missing_malformed_ambiguous_and_foreign_native_associations_are_skipped(self):
        valid = native_run(1)["pull_requests"][0]
        invalid_associations = [None, [], {}, "90", [None], [valid, valid]]
        for number in (None, True, 0, -1, "90", "90/../../actions/runs"):
            invalid_associations.append([{**valid, "number": number}])
        for url in (
            None, "https://api.github.com/repos/other/repo/pulls/90",
            f"https://api.github.com/repos/{lifecycle.CI_REPO}/pulls/91",
            f"https://github.com/{lifecycle.CI_REPO}/pull/90",
        ):
            invalid_associations.append([{**valid, "url": url}])
        for base in (
            None, {}, {"repo": None}, {"repo": {"url": "https://api.github.com/repos/other/repo"}},
            {"repo": {"url": f"https://api.github.com/repos/{lifecycle.CI_REPO}.other"}},
        ):
            invalid_associations.append([{**valid, "base": base}])
        for association in invalid_associations:
            with self.subTest(association=association):
                self.api.calls.clear()
                candidate = native_run(1)
                candidate["pull_requests"] = association
                self.api.runs[lifecycle.CI_REPO] = [candidate]
                self.assertEqual(self.cleanup(), [])
                self.assertFalse(any("/pulls/" in path for _, path in self.api.calls))

    def test_native_run_is_rechecked_for_retry_completion_and_association_changes(self):
        original = native_run(1)
        changed_event = {**original, "event": "push"}
        missing_association = {**original, "pull_requests": []}
        for current in (
            native_run(1, attempt=2), native_run(1, status="completed"),
            native_run(1, pr=91), changed_event, missing_association,
        ):
            with self.subTest(current=current):
                self.api.calls.clear()
                self.api.runs[lifecycle.CI_REPO] = [original]
                self.api.current[1] = current
                self.assertEqual(self.cleanup(), [])
                self.assertEqual(self.api.cancellations, [])

    def test_native_cancellation_does_not_authorize_a_private_image_cancellation(self):
        for workflow in ("workflow_checks.yaml", "cloud_integration_ci.yaml"):
            with self.subTest(workflow=workflow):
                native = native_run(1, workflow=workflow)
                self.api.runs[lifecycle.CI_REPO] = [native]
                self.api.parents[(1, 1)] = {**native, "status": "completed", "conclusion": "cancelled"}
                self.api.runs[lifecycle.CLOUD_REPO] = [image(10, 1)]
                self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, 1)])

    def test_native_pr_pagination_and_other_pr_are_preserved(self):
        self.api.runs[lifecycle.CI_REPO] = [native_run(index) for index in range(1, 102)]
        self.api.runs[lifecycle.CI_REPO].append(native_run(102, pr=91))
        self.api.prs[(lifecycle.CI_REPO, 91)] = {"state": "open", "merged": False}
        self.assertEqual(self.cleanup(), [(lifecycle.CI_REPO, index) for index in range(1, 102)])
        self.assertEqual(sum("/pulls/" in path for _, path in self.api.calls), 2)

    def test_native_dry_run_does_not_write(self):
        self.api.runs[lifecycle.CI_REPO] = [native_run(1)]
        self.assertEqual(self.cleanup(apply=False), [(lifecycle.CI_REPO, 1)])
        self.assertEqual(self.api.cancellations, [])

    def test_rejects_unexpected_source_and_invalid_pr_before_request(self):
        for source, number in (
            ("evil/repo", 123), (lifecycle.CLOUD_REPO, "123/../../runs"),
            (CLIENT_REPO, True), (lifecycle.CI_REPO, 0),
        ):
            with self.assertRaises(ValueError):
                lifecycle.pr_is_merged(source, number, self.api.request)
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

    def test_old_dispatcher_finds_v2_workflows_without_parent_metadata(self):
        self.api.runs[lifecycle.CI_REPO] = [
            run(1, workflow="cloud_rustlint_ci.yaml"), run(2, parent=(9, 2)),
        ]
        self.assertEqual(
            lifecycle.find_children(
                ["rustlint", "integration"], (9, 2), self.api.request,
                now=iter((0, 181)).__next__,
            ),
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


if __name__ == "__main__":
    unittest.main()
