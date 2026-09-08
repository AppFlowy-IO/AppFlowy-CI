"""Keep destructive snapshot cleanup after every ordinary Cloud integration test."""

from pathlib import Path
import unittest

import yaml


class FinalAccountDeletionTest(unittest.TestCase):
    def test_no_ordinary_tests_run_after_seeded_account_deletion(self):
        workflow = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
        jobs = yaml.safe_load(workflow.read_text())["jobs"]
        integration = next(job for job in jobs.values() if "strategy" in job)
        steps = integration["steps"]
        final_steps = [
            index for index, step in enumerate(steps)
            if "script/test_final_account_deletion.sh" in step.get("run", "")
        ]
        self.assertEqual(len(final_steps), 1)
        final_index = final_steps[0]
        test_indices = [
            index for index, step in enumerate(steps)
            if "cargo test" in step.get("run", "")
        ]
        self.assertTrue(test_indices)
        self.assertTrue(all(index < final_index for index in test_indices))
        self.assertNotIn("always()", steps[final_index].get("if", ""))
        self.assertNotIn("continue-on-error", steps[final_index])


if __name__ == "__main__":
    unittest.main()
