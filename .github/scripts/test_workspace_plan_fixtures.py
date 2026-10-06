"""Cloud member fixtures must address the same disposable PostgreSQL as the server."""

from pathlib import Path
import unittest
from urllib.parse import urlsplit

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


class WorkspacePlanFixtureWorkflowTest(unittest.TestCase):
    def test_flutter_and_rust_member_fixtures_receive_explicit_test_database(self):
        for filename, job_name, test_name in [
            ("flutter_ci.yaml", "cloud_integration_test", "Run Flutter integration tests ("),
            ("rust_ci.yaml", "test", "Run Tests"),
        ]:
            with self.subTest(workflow=filename):
                job = yaml.safe_load((WORKFLOWS / filename).read_text())["jobs"][job_name]
                connection = urlsplit(job.get("env", {}).get("APPFLOWY_TEST_DATABASE_URL", ""))
                self.assertEqual(connection.scheme, "postgres")
                self.assertEqual(connection.hostname, "127.0.0.1")
                self.assertEqual(connection.port, 5432)
                self.assertEqual(connection.path, "/postgres")
                setup = next(i for i, step in enumerate(job["steps"])
                             if step.get("name") == "Install workspace plan fixture client")
                tests = next(i for i, step in enumerate(job["steps"])
                             if step.get("name", "").startswith(test_name))
                self.assertLess(setup, tests)
                self.assertIn("postgresql-client", job["steps"][setup]["run"])


if __name__ == "__main__":
    unittest.main()
