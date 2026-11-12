from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

import commercial_scim_runner as runner


class CommercialScimTest(unittest.TestCase):
    def exercise(self, *, missing=False, ignored=False, passed=3, execution_exit=0):
        calls = []
        names = sorted(runner.REQUIRED)
        if missing:
            names.pop()

        def invoke(command, **kwargs):
            calls.append(command)
            self.assertIn("--features=commercial-ci", command)
            self.assertIn("main", command)
            if "--list" in command:
                selected = names if "--ignored" not in command or ignored else []
                output = "\n".join(name + ": test" for name in selected)
                code = 0
            else:
                output = f"test result: ok. {passed} passed; 0 failed; 0 ignored;"
                code = execution_exit
            return subprocess.CompletedProcess(command, code, output)

        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            source = Path(temporary)
            path = source / "tests/commercial_test/scim_test/identity_recovery.rs"
            path.parent.mkdir(parents=True)
            path.touch()
            runner.run_contracts(source, invoke)
        return calls

    def test_all_three_contracts_execute_serially(self):
        calls = self.exercise()
        self.assertEqual(len(calls), 3)
        self.assertIn("--test-threads=1", calls[-1])
        self.assertNotIn("--ignored", calls[-1])

    def test_missing_ignored_unexecuted_and_failed_tests_fail_the_job(self):
        for options in ({"missing": True}, {"ignored": True}, {"passed": 0},
                        {"passed": 2}, {"execution_exit": 7}):
            with self.subTest(options=options), self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                self.exercise(**options)

    def test_older_refs_are_supported_but_new_production_code_requires_tests(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            source = Path(temporary)
            runner.run_contracts(source, lambda *a, **kw: self.fail("old source must not run"))
            marker = source / "libs/appflowy-cloud-workspace-support/src/biz/user/user_identity/guest.rs"
            marker.parent.mkdir(parents=True)
            marker.touch()
            with self.assertRaises(RuntimeError):
                runner.run_contracts(source)

    def test_workflow_runs_owned_contracts_once_before_generic_commercial_tests(self):
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((root / ".github/workflows/cloud_commercial_integration_ci.yaml").read_text())
        steps = workflow["jobs"]["test"]["steps"]
        owned = next(i for i, step in enumerate(steps) if step.get("name") == "Run SCIM identity recovery contracts")
        generic = next(i for i, step in enumerate(steps) if step.get("name") == "Run Tests")
        self.assertLess(owned, generic)
        self.assertEqual(steps[owned]["if"], "matrix.test_service == 'appflowy_cloud_commercial'")
        self.assertIn("--skip " + runner.MODULE, steps[generic]["run"])


if __name__ == "__main__":
    unittest.main()
