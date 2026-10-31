import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

import commercial_presentation_runner as runner


class CommercialPresentationTest(unittest.TestCase):
    def test_workflow_uses_self_host_image_and_dedicated_guard_before_generic_suite(self):
        repository = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((repository / ".github/workflows/cloud_commercial_integration_ci.yaml").read_text())
        builds = "\n".join(step.get("run", "") for step in workflow["jobs"]["setup"]["steps"])
        self.assertIn('FEATURES="self-host-af, commercial-ci, test-mailer"', builds)
        self.assertIn("--build-arg FEATURES=self-host-af appflowy_worker appflowy_search appflowy_mcp ai", builds)
        self.assertIn("appflowy_mcp appflowy_ai; do", builds)
        steps = workflow["jobs"]["test"]["steps"]
        guarded = next(i for i, step in enumerate(steps)
                       if "commercial_presentation_runner.py" in step.get("run", ""))
        generic = next(i for i, step in enumerate(steps) if step.get("name") == "Run Tests")
        self.assertLess(guarded, generic)
        self.assertEqual(steps[guarded]["if"], "matrix.test_service == 'appflowy_cloud_commercial'")
        self.assertIn("--skip commercial_test::presentation_test::", steps[generic]["run"])

    def exercise(self, *, missing=False, ignored=False, passed=None):
        calls = []
        selected = set(runner.REQUIRED)
        if missing:
            selected.pop()

        def run(command, **kwargs):
            calls.append(command)
            self.assertIn("--features=commercial-ci", command)
            self.assertEqual(kwargs["env"]["APPFLOWY_PRESENTATION_COMMERCIAL_OWNED_FIXTURE"], "1")
            if "--list" in command:
                names = selected if "--ignored" not in command or ignored else set()
                output = "\n".join(name + ": test" for name in sorted(names))
            else:
                count = len(selected) if passed is None else passed
                output = f"test result: ok. {count} passed; 0 failed; 0 ignored;"
            return subprocess.CompletedProcess(command, 0, output)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            tests = source / "tests/commercial_test/presentation_test.rs"
            tests.parent.mkdir(parents=True)
            tests.touch()
            with patch.dict("os.environ", APPFLOWY_PRESENTATION_COMMERCIAL_OWNED_FIXTURE="1"):
                runner.run_contracts(source, run)
        return calls

    def test_real_server_suite_runs_serially_and_requires_every_test(self):
        calls = self.exercise()
        self.assertIn("--test-threads=1", calls[-1])
        self.assertNotIn("--ignored", calls[-1])
        self.assertEqual(len(calls), 3)

    def test_missing_ignored_or_unexecuted_bodies_fail(self):
        for case in ({"missing": True}, {"ignored": True}, {"passed": 0}, {"passed": 2}):
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                self.exercise(**case)

    def test_current_production_source_cannot_silently_lose_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            runner.run_contracts(source, lambda *a, **kw: self.fail("old source must not run"))
            marker = source / "libs/appflowy-cloud-ai/src/biz/presentation/source/resolver/read_policy.rs"
            marker.parent.mkdir(parents=True)
            marker.touch()
            with self.assertRaises(RuntimeError):
                runner.run_contracts(source)

    def test_license_test_requires_explicit_disposable_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            tests = source / "tests/commercial_test/presentation_test.rs"
            tests.parent.mkdir(parents=True)
            tests.touch()
            with patch.dict("os.environ", {}, clear=True), self.assertRaises(RuntimeError):
                runner.run_contracts(source, lambda *a, **kw: self.fail("must not start tests"))


if __name__ == "__main__":
    unittest.main()
