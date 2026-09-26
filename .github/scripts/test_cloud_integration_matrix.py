"""Keep search assertions independent from large database-history indexing backlogs."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
SEARCH_RESTORE = "database::database_history_test::search_restore"
DATABASE_INDEX = "database::database_index_test"


class DatabaseIndexIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        cls.integration = next(job for job in jobs.values() if "strategy" in job)
        cls.lanes = cls.integration["strategy"]["matrix"]["include"]

    def test_search_restore_runs_once_on_the_clean_database_index_stack(self):
        script = next(
            step["run"] for step in self.integration["steps"]
            if step.get("name") == "Run Tests"
        )
        executions = {DATABASE_INDEX: [], SEARCH_RESTORE: []}
        for service in ("appflowy_cloud_database", "appflowy_cloud_database_index"):
            lane = next(lane for lane in self.lanes if lane["test_service"] == service)
            rendered = re.sub(
                r"\$\{\{ matrix\.(\w+) \}\}",
                lambda match: str(lane.get(match[1], "")),
                script,
            )
            with self.subTest(service=service), tempfile.TemporaryDirectory() as directory:
                calls = Path(directory) / "cargo-calls"
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", (
                        'cargo() { printf "%s\\t" "$@" >> "$CARGO_CALLS"; '
                        'printf "\\n" >> "$CARGO_CALLS"; }\n' + rendered
                    )],
                    env={
                        **os.environ,
                        "CARGO_CALLS": str(calls),
                        "TEST_SKIPS": lane.get("test_skips", ""),
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                commands = [line.rstrip("\t").split("\t") for line in calls.read_text().splitlines()]
                for command in commands:
                    delimiter = command.index("--")
                    filters = [argument for argument in command[:delimiter] if argument.endswith("::")]
                    self.assertEqual(len(filters), 1, command)
                    skips = [
                        command[index + 1] for index, argument in enumerate(command[:-1])
                        if argument == "--skip"
                    ]
                    for module in executions:
                        test_name = f"{module}::regression"
                        if test_name.startswith(filters[0]) and not any(skip in test_name for skip in skips):
                            executions[module].append(service)

        self.assertEqual(executions, {
            DATABASE_INDEX: ["appflowy_cloud_database_index"],
            SEARCH_RESTORE: ["appflowy_cloud_database_index"],
        })


if __name__ == "__main__":
    unittest.main()
