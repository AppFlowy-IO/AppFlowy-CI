"""Exercise cache-suite discovery and result reporting from the actual CI workflow."""

import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
SUITE_FILES = (
    "script/test_encoded_cache.sh",
    "script/test_encoded_cache_dashboard.py",
    "script/test_encoded_cache_compose.py",
)
CACHE_JOBS = ("cache-contracts", "cache-observability")
REPORTED_JOBS = (
    "verify_test_module_coverage",
    "build_cloud",
    "build_worker",
    "build_search",
    "build_mcp",
    "test",
    *CACHE_JOBS,
)


def step_by_id(job, step_id):
    return next(step for step in job["steps"] if step.get("id") == step_id)


def run_step(step, directory, **extra_env):
    """Run the checked-in script without GitHub services or inherited credentials."""
    output = directory / "github-output"
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(directory / "github-summary"),
        **extra_env,
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    outputs = {}
    if output.exists():
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return result, outputs


class EncodedCacheWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.jobs = cls.workflow["jobs"]

    def detect_suite(self, files):
        step = step_by_id(self.jobs["verify_test_module_coverage"], "encoded_cache")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for file in files:
                path = directory / file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            return run_step(step, directory)

    def test_older_cloud_ref_explicitly_disables_absent_suite(self):
        result, outputs = self.detect_suite(())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("enabled"), "false")

    def test_complete_suite_is_enabled(self):
        result, outputs = self.detect_suite(SUITE_FILES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("enabled"), "true")

    def test_every_partial_suite_fails_instead_of_silently_skipping(self):
        for count in range(1, len(SUITE_FILES)):
            for files in itertools.combinations(SUITE_FILES, count):
                with self.subTest(files=files):
                    result, outputs = self.detect_suite(files)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotEqual(outputs.get("enabled"), "true")

    def test_detection_and_jobs_use_the_private_cloud_checkout(self):
        verification = self.jobs["verify_test_module_coverage"]
        detection = step_by_id(verification, "encoded_cache")
        self.assertEqual(detection["working-directory"], "appflowy-cloud-premium")
        self.assertEqual(
            verification["outputs"]["encoded_cache_available"],
            "${{ steps.encoded_cache.outputs.enabled }}",
        )
        private_checkout = next(
            step for step in verification["steps"]
            if step.get("with", {}).get("repository") == "AppFlowy-IO/AppFlowy-Cloud-Premium"
        )
        for name in CACHE_JOBS:
            with self.subTest(job=name):
                job = self.jobs[name]
                dependencies = job["needs"]
                if isinstance(dependencies, str):
                    dependencies = [dependencies]
                self.assertIn("verify_test_module_coverage", dependencies)
                self.assertIn(
                    "needs.verify_test_module_coverage.outputs.encoded_cache_available == 'true'",
                    job["if"],
                )
                checkout = next(
                    step for step in job["steps"]
                    if step.get("with", {}).get("repository")
                    == "AppFlowy-IO/AppFlowy-Cloud-Premium"
                )
                self.assertEqual(checkout["with"]["ref"], private_checkout["with"]["ref"])
                self.assertEqual(checkout["with"]["token"], "${{ secrets.ADMIN_GITHUB_TOKEN }}")
                self.assertNotIn("continue-on-error", job)

    def test_parallel_cache_contracts_keep_database_locale_and_test_runner(self):
        job = self.jobs["cache-contracts"]
        self.assertEqual(job["services"]["postgres"]["image"], "pgvector/pgvector:pg15")
        self.assertIn("5432:5432", job["services"]["postgres"]["ports"])
        self.assertEqual(str(job["env"]["APPFLOWY_CACHE_TEST_THREADS"]), "4")
        self.assertEqual(str(job["env"]["SQLX_OFFLINE"]).lower(), "true")
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("protobuf-compiler", commands)
        self.assertIn("redis-server", commands)
        self.assertIn("locale-gen en_US.UTF-8", commands)
        runner = next(
            step for step in job["steps"]
            if "bash script/test_encoded_cache.sh" in step.get("run", "")
        )
        self.assertNotIn("continue-on-error", runner)
        self.assertNotIn("|| true", runner["run"])

    def test_observability_runs_both_checks_with_verified_prometheus(self):
        job = self.jobs["cache-observability"]
        self.assertEqual(job["env"]["PROMETHEUS_VERSION"], "2.55.1")
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("sha256sum --check --strict", commands)
        self.assertIn("promtool", commands)
        for script in SUITE_FILES[1:]:
            runner = next(
                step for step in job["steps"]
                if f"python3 {script}" in step.get("run", "")
            )
            self.assertNotIn("continue-on-error", runner)
            self.assertNotIn("|| true", runner["run"])

    def test_notification_waits_for_every_gate_and_reports_aggregated_result(self):
        job = self.jobs["notify-webhook"]
        self.assertTrue(set(REPORTED_JOBS).issubset(job["needs"]))
        self.assertEqual(job["if"], "always()")
        result = step_by_id(job, "result")
        self.assertEqual(result["env"]["NEEDS_JSON"], "${{ toJSON(needs) }}")
        dispatch = next(
            step for step in job["steps"]
            if step.get("uses", "").startswith("peter-evans/repository-dispatch@")
        )
        payload = dispatch["with"]["client-payload"]
        self.assertIn('"status": "${{ steps.result.outputs.status }}"', payload)
        self.assertNotIn("needs.test.result", payload)

    def aggregate(self, overrides):
        needs = {job: {"result": "success"} for job in REPORTED_JOBS}
        for job, result in overrides.items():
            needs[job]["result"] = result
        step = step_by_id(self.jobs["notify-webhook"], "result")
        with tempfile.TemporaryDirectory() as temporary:
            result, outputs = run_step(step, Path(temporary), NEEDS_JSON=json.dumps(needs))
        self.assertEqual(result.returncode, 0, result.stderr)
        return outputs.get("status")

    def test_each_cache_failure_overrides_successful_integration_tests(self):
        for job in CACHE_JOBS:
            with self.subTest(job=job):
                self.assertEqual(self.aggregate({job: "failure"}), "failure")

    def test_absent_legacy_suite_does_not_fail_successful_integration_tests(self):
        self.assertEqual(self.aggregate({job: "skipped" for job in CACHE_JOBS}), "success")

    def test_setup_and_build_failures_cannot_be_hidden_by_skipped_tests(self):
        for job in REPORTED_JOBS[:5]:
            with self.subTest(job=job):
                self.assertEqual(
                    self.aggregate({job: "failure", "test": "skipped"}), "failure"
                )

    def test_cancellation_is_reported_unless_another_gate_failed(self):
        self.assertEqual(self.aggregate({"cache-contracts": "cancelled"}), "cancelled")
        self.assertEqual(
            self.aggregate({"cache-contracts": "cancelled", "test": "failure"}), "failure"
        )

    def test_integration_result_is_preserved_when_other_gates_succeed(self):
        for result in ("success", "failure", "cancelled", "skipped"):
            with self.subTest(result=result):
                self.assertEqual(self.aggregate({"test": result}), result)


if __name__ == "__main__":
    unittest.main()
