"""Regression coverage for a receiver deadline expiring before CI finishes."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

import ci_workflow_results as results


ROOT = Path(__file__).resolve().parents[2]


def workflow(key, run_id, status, conclusion, **extra):
    return {"key": key, "name": results.WORKFLOW_NAMES[key], "run_id": run_id,
            "url": f"https://github.com/{results.CI_REPO}/actions/runs/{run_id}",
            "status": status, "conclusion": conclusion, **extra}


class WorkflowResultsTest(unittest.TestCase):
    def test_observed_queued_flutter_and_running_ios_are_pending(self):
        observed = [workflow("flutter", 42711818282, "queued", None),
                    workflow("rust", 42711819194, "completed", "success"),
                    workflow("ios", 42711820231, "in_progress", None)]
        report = results.render_report(observed, "1564", expired=True)
        self.assertIn("CI is still pending", report)
        self.assertIn("Pending workflows", report)
        self.assertIn("not a test timeout", report)
        self.assertIn("Automatic approval was withheld", report)
        self.assertIn(observed[0]["url"], report)
        self.assertIn(observed[2]["url"], report)
        self.assertNotIn("Failed workflows", report)
        self.assertNotIn("Rust Testing", report)
        self.assertFalse(results.all_passed(observed))

    def test_failed_matrix_job_is_reported_while_siblings_are_running(self):
        job = {"id": 10, "name": "integration_test (26)", "html_url": "https://github.com/job/10",
               "conclusion": "failure", "runner_name": "runner-1",
               "steps": [{"name": "Run tests", "conclusion": "failure"}]}
        observed = [workflow("flutter", 1, "in_progress", None, jobs=[job]),
                    workflow("ios", 2, "queued", None)]
        report = results.render_report(observed, "1564", True,
                                       request=lambda *args, **kwargs: "2026-11-10T00:00:00Z Error: real failure")
        self.assertIn("CI failures detected", report)
        self.assertIn("Other jobs in this workflow are still pending", report)
        self.assertIn("Pending workflows", report)
        self.assertIn("Failed step: Run tests", report)
        self.assertIn("Error: real failure", report)
        self.assertIn(job["html_url"], report)

    def test_terminal_non_successes_never_pass(self):
        for conclusion in ("failure", "timed_out", "cancelled", "skipped", "neutral", None):
            with self.subTest(conclusion=conclusion):
                observed = [workflow("rust", 1, "completed", conclusion)]
                self.assertFalse(results.all_passed(observed))
                report = results.render_report(observed, "42", False)
                self.assertIn("Automatic approval was withheld", report)
                self.assertNotIn("Pending workflows", report)
        self.assertEqual(results.category(workflow("rust", 1, "completed", "timed_out")), "failed")
        self.assertEqual(results.category(workflow("rust", 1, "completed", None)), "unverified")

    def test_only_explicit_complete_successes_authorize_approval(self):
        self.assertFalse(results.all_passed([]))
        self.assertFalse(results.all_passed([workflow("rust", 1, "in_progress", "success")]))
        self.assertTrue(results.all_passed([workflow("rust", 1, "completed", "success"),
                                           workflow("ios", 2, "completed", "success")]))

    def test_missing_and_unknown_workflows_remain_unverified(self):
        requested = results.requested_workflows({"WORKFLOWS": "rust, flutter,unrecognized,rust",
                                                 "RUST_RUN_URL": workflow("rust", 1, "", "")["url"]})
        self.assertEqual(len(requested), 3)
        observed = results.observe(requested, lambda path: {"id": 1, "status": "completed", "conclusion": "success"})
        self.assertFalse(results.all_passed(observed))
        report = results.render_report(observed, "42", True)
        self.assertIn("No valid dispatched run URL", report)
        self.assertIn("Unknown requested workflow", report)
        self.assertIn("Unverified workflows", report)
        self.assertNotIn("Failed workflows", report)

    def test_api_failure_and_wrong_run_cannot_authorize_approval(self):
        requested = results.requested_workflows({"WORKFLOWS": "rust", "RUST_RUN_URL": workflow("rust", 1, "", "")["url"]})
        def unavailable(path):
            raise results.ApiError("temporarily unavailable")
        for request in (unavailable, lambda path: {"id": 2, "status": "completed", "conclusion": "success"}):
            with self.subTest(request=request):
                observed = results.observe(requested, request)
                self.assertFalse(results.all_passed(observed))
                self.assertEqual(results.category(observed[0]), "unverified")

    def test_wait_recovers_from_transient_api_failure(self):
        requested = [workflow("rust", 1, None, None)]
        clock = [0]
        calls = []
        def request(path):
            calls.append(path)
            if len(calls) == 1:
                raise results.ApiError("temporary")
            return {"id": 1, "status": "completed", "conclusion": "success"}
        def sleep(seconds):
            clock[0] += seconds
        with redirect_stdout(io.StringIO()):
            observed, expired = results.wait_for_results(requested, max_wait=60, interval=30,
                                                         request=request, clock=lambda: clock[0], sleep=sleep)
        self.assertFalse(expired)
        self.assertTrue(results.all_passed(observed))
        self.assertNotIn("error", observed[0])

    def test_wait_budget_includes_api_time(self):
        requested = [workflow("rust", 1, None, None)]
        clock = [0]
        sleeps = []
        def request(path):
            clock[0] += 8
            return {"id": 1, "status": "queued", "conclusion": None}
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with redirect_stdout(io.StringIO()):
            observed, expired = results.wait_for_results(requested, max_wait=10, interval=30,
                                                         request=request, clock=lambda: clock[0], sleep=sleep)
        self.assertTrue(expired)
        self.assertEqual(sleeps, [2])
        self.assertEqual(results.category(observed[0]), "pending")

    def test_all_job_pages_are_inspected_for_failed_matrix_jobs(self):
        failed = {"id": 101, "name": "last matrix job", "conclusion": "failure"}
        calls = []
        def request(path):
            calls.append(path)
            return {"jobs": ([{"id": number, "conclusion": "success"} for number in range(100)]
                             if path.endswith("&page=1") else [failed])}
        observed = [workflow("flutter", 1, "in_progress", None)]
        results.inspect_jobs(observed, request)
        self.assertEqual(len(calls), 2)
        self.assertIn("filter=latest", calls[0])
        self.assertIn(failed, observed[0]["jobs"])
        self.assertEqual(results.category(observed[0]), "failed")

    def test_job_api_errors_do_not_turn_pending_runs_into_failures(self):
        def unavailable(*args, **kwargs):
            raise results.ApiError("Jobs unavailable")
        observed = [workflow("flutter", 1, "queued", None)]
        results.inspect_jobs(observed, unavailable)
        report = results.render_report(observed, "42", True)
        self.assertIn("CI is still pending", report)
        self.assertIn("Jobs unavailable", report)
        self.assertNotIn("Failed workflows", report)

    def test_actual_timed_out_job_retains_link_when_logs_are_unavailable(self):
        def unavailable(*args, **kwargs):
            raise results.ApiError("Logs unavailable")
        observed = [workflow("ios", 1, "completed", "timed_out", jobs=[
            {"id": 10, "name": "iOS tests", "html_url": "https://github.com/job/10", "conclusion": "timed_out"}])]
        report = results.render_report(observed, "42", False, unavailable)
        self.assertIn("CI failures detected", report)
        self.assertIn("[timed_out](https://github.com/job/10)", report)
        self.assertIn("Job logs are not available", report)

    def test_timestamped_rust_and_flutter_logs_keep_test_diagnostics(self):
        rust = "2026-11-10T00:00:00Z \x1b[31mtest row_count ... FAILED\x1b[0m\n"
        self.assertEqual(results.failure_excerpt(rust, "rust"), ["test row_count ... FAILED"])
        self.assertEqual(results.failure_excerpt("2026-11-10T00:00:00Z Error: video stalled", "flutter"),
                         ["Error: video stalled"])

    def test_receiver_checks_out_its_exact_revision_and_forwards_all_requested_lanes(self):
        steps = yaml.safe_load((ROOT / ".github/workflows/webhook_receiver.yaml").read_text())["jobs"]["auto-approve-pr"]["steps"]
        wait = next(step for step in steps if step.get("name") == "Wait for CI workflows to complete")
        checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
        self.assertLess(steps.index(checkout), steps.index(wait))
        self.assertEqual(checkout["with"]["ref"], "${{ github.workflow_sha }}")
        self.assertEqual(wait["run"], "python3 ci-tools/.github/scripts/ci_workflow_results.py")
        self.assertEqual(wait["env"]["GH_TOKEN"], "${{ secrets.ADMIN_GITHUB_TOKEN }}")
        for key in results.WORKFLOW_NAMES:
            self.assertIn(f"{key.upper()}_RUN_URL", wait["env"])
        approval = next(step for step in steps if step.get("name") == "Auto-approve PR if all tests pass")
        self.assertEqual(approval["if"], "env.all_workflows_passed == 'true'")
        report = steps[-1]
        self.assertEqual(report["if"], "env.all_workflows_passed == 'false'")
        self.assertIn("jq -Rs . < ci-results.md", report["run"])

    def test_cli_with_fixture_api_writes_pending_report_and_blocks_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            fixture = directory / "fixture.json"
            fixture.write_text(json.dumps({
                f"repos/{results.CI_REPO}/actions/runs/1": {"id": 1, "status": "queued", "conclusion": None},
                f"repos/{results.CI_REPO}/actions/runs/1/jobs?filter=latest&per_page=100&page=1": {"jobs": []},
            }))
            gh = directory / "gh"
            gh.write_text(f"#!{sys.executable}\n" +
                          "import json, os, sys\nfrom pathlib import Path\n"
                          "assert sys.argv[1] == 'api'\n"
                          "print(json.dumps(json.loads(Path(os.environ['RESULTS_FIXTURE']).read_text())[sys.argv[2]]))\n")
            gh.chmod(0o755)
            output = directory / "github-env"
            report = directory / "report.md"
            process = subprocess.run([sys.executable, "-B", str(ROOT / ".github/scripts/ci_workflow_results.py"),
                                      "--max-wait", "0", "--report", str(report)],
                                     env={**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
                                          "RESULTS_FIXTURE": str(fixture), "GH_TOKEN": "test-owned-token",
                                          "GITHUB_ENV": str(output), "PR_NUMBER": "1564", "WORKFLOWS": "flutter",
                                          "FLUTTER_RUN_URL": workflow("flutter", 1, "", "")["url"]},
                                     text=True, capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(output.read_text(), "all_workflows_passed=false\n")
            self.assertIn("CI is still pending", report.read_text())
            self.assertNotIn("Failed workflows", report.read_text())


if __name__ == "__main__":
    unittest.main()
