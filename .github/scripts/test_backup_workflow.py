"""Exercise Backup capability detection and integration result reporting."""

import json
from pathlib import Path
import tempfile
import unittest

import yaml

from test_encoded_cache_workflow import run_step, step_by_id


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
SUITE_FILES = ("docker-compose-backup.yml", "script/ci/test_backup.sh")


class BackupWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.jobs = cls.workflow["jobs"]

    def detect(self, files):
        step = step_by_id(self.jobs["image_source"], "backup")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in files:
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            return run_step(step, directory)

    def test_legacy_cloud_without_backup_explicitly_skips_category(self):
        result, outputs = self.detect(())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs, {"enabled": "false"})
        self.assertIn("predates", result.stdout)

    def test_complete_backup_stack_enables_category(self):
        result, outputs = self.detect(SUITE_FILES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs, {"enabled": "true"})

    def test_partial_backup_stack_fails_instead_of_skipping(self):
        for name in SUITE_FILES:
            with self.subTest(file=name):
                result, outputs = self.detect((name,))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs, {})
                self.assertIn("Incomplete Backup integration stack", result.stdout)

    def test_capability_comes_from_the_same_checkout_as_source_sha(self):
        job = self.jobs["image_source"]
        self.assertEqual(job["outputs"]["backup_available"],
                         "${{ steps.backup.outputs.enabled }}")
        steps = job["steps"]
        checkout = next(i for i, step in enumerate(steps)
                        if step.get("uses", "").startswith("actions/checkout@"))
        pinned = next(i for i, step in enumerate(steps) if step.get("id") == "source")
        detection = next(i for i, step in enumerate(steps) if step.get("id") == "backup")
        self.assertLess(checkout, pinned)
        self.assertLess(pinned, detection)
        self.assertNotIn("working-directory", steps[detection])

    def test_backup_category_waits_for_exact_source_and_completed_image_build(self):
        job = self.jobs["backup"]
        self.assertEqual(job["name"], "Integration Tests (Backup)")
        self.assertEqual(set(job["needs"]), {"image_source", "build_self_hosted"})
        for requirement in (
            "needs.image_source.result == 'success'",
            "needs.image_source.outputs.backup_available == 'true'",
            "needs.build_self_hosted.result == 'success'",
        ):
            self.assertIn(requirement, job["if"])
        self.assertNotIn("github.event_name == 'workflow_dispatch'", job["if"])
        self.assertNotIn("continue-on-error", job)
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(job["timeout-minutes"], 360)
        runner = step_by_id(job, "qualification")
        self.assertEqual(runner["env"]["SOURCE_SHA"], "${{ needs.image_source.outputs.sha }}")
        self.assertEqual(runner["env"]["SOURCE_REF"], "${{ needs.image_source.outputs.ref }}")
        self.assertFalse(any(step.get("uses", "").startswith("actions/upload-artifact@")
                             for step in job["steps"]))
        self.assertEqual(runner["run"], "python3 .github/scripts/request_backup_tests.py")
        self.assertNotIn("continue-on-error", runner)

    def test_backup_failure_is_not_hidden_by_successful_ordinary_tests(self):
        notification = self.jobs["notify-webhook"]
        self.assertIn("backup", notification["needs"])
        step = step_by_id(notification, "result")
        for backup, expected in (("success", "success"), ("failure", "failure"),
                                 ("cancelled", "cancelled"), ("skipped", "success")):
            with self.subTest(result=backup), tempfile.TemporaryDirectory() as temporary:
                needs = {name: {"result": "success"} for name in notification["needs"]}
                needs["backup"]["result"] = backup
                result, outputs = run_step(step, Path(temporary), NEEDS_JSON=json.dumps(needs))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outputs.get("status"), expected)


    def test_failed_predecessors_cannot_report_success_when_backup_is_skipped(self):
        notification = self.jobs["notify-webhook"]
        step = step_by_id(notification, "result")
        cases = (
            ({"image_source": "failure", "build_self_hosted": "skipped", "test": "skipped",
              "backup": "skipped"}, "failure"),
            ({"build_self_hosted": "failure", "test": "skipped", "backup": "skipped"},
             "failure"),
            ({"build_self_hosted": "cancelled", "test": "skipped", "backup": "skipped"},
             "cancelled"),
            ({"backup": "skipped"}, "success"),
        )
        for overrides, expected in cases:
            with self.subTest(results=overrides), tempfile.TemporaryDirectory() as temporary:
                needs = {name: {"result": "success"} for name in notification["needs"]}
                for name, result in overrides.items():
                    needs[name]["result"] = result
                result, outputs = run_step(step, Path(temporary), NEEDS_JSON=json.dumps(needs))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outputs["status"], expected)



if __name__ == "__main__":
    unittest.main()
