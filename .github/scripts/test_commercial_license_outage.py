import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/cloud_commercial_integration_ci.yaml"
FILES = ("script/ci/license_outage_fixture.py", "tests/commercial_test/billing_outage.rs",
         "script/ci/run_license_outage_ci.py")
LANE = "appflowy_cloud_license_outage"


class LicenseOutageShard(unittest.TestCase):
    def setUp(self):
        self.jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]

    def select(self, paths):
        selector = next(step for step in self.jobs["setup"]["steps"] if step.get("id") == "shards")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in paths:
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            output = root / "output"
            result = subprocess.run(["bash", "-eu", "-c", selector["run"]], cwd=root,
                                    env=dict(os.environ, GITHUB_OUTPUT=str(output)),
                                    capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return json.loads(output.read_text().removeprefix("services="))

    def test_old_and_complete_cloud_revisions_select_their_contracts(self):
        old = ["appflowy_cloud_commercial", "appflowy_cloud_whitelist"]
        self.assertEqual(self.select([]), old)
        self.assertEqual(self.select(FILES), old + [LANE])

    def test_partial_delivery_is_a_ci_error(self):
        for missing in FILES:
            with self.subTest(missing=missing), self.assertRaisesRegex(RuntimeError, "incomplete"):
                self.select([name for name in FILES if name != missing])

    def test_only_dedicated_runner_owns_the_database_and_cloud_restart(self):
        job = self.jobs["test"]
        self.assertIn("needs.setup.outputs.test_services", job["strategy"]["matrix"]["test_service"])
        self.assertIn("ubuntu-24.04", job["runs-on"])
        steps = {step.get("name"): step for step in job["steps"]}
        case = [step for step in job["steps"] if step.get("run", "").strip() == f"python3 -B {FILES[-1]}"]
        self.assertEqual(len(case), 1)
        self.assertEqual(case[0]["if"], f"matrix.test_service == '{LANE}'")
        self.assertEqual(case[0]["env"]["COMMERCIAL_LICENSE_OUTAGE_SHARD"], "1")
        for name in ("Run Docker-Compose", "Wait for services to be ready", "Run Tests", "Report commercial container status"):
            self.assertIn(f"matrix.test_service != '{LANE}'", steps[name]["if"])
        images = steps["Download private images for this shard"]["with"]
        self.assertIn("commercial-cloud-images", images["pattern"])
        self.assertIn("commercial-*-images", images["pattern"])
        self.assertEqual(images["repository"], "AppFlowy-IO/AppFlowy-Cloud-Premium")
        self.assertEqual(steps["Download the private dependency manifest"]["with"]["repository"],
                         images["repository"])
        for job in self.jobs.values():
            for step in job["steps"]:
                self.assertNotIn("upload-artifact", step.get("uses", ""))
                self.assertNotIn("docker save", step.get("run", ""))
        self.assertEqual(steps["Checkout Private Repo"]["with"]["ref"], "${{ needs.setup.outputs.cloud_sha }}")


if __name__ == "__main__":
    unittest.main()
