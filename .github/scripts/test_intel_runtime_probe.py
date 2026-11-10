"""A successful process is insufficient without completed, fresh rendering work."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

import intel_runtime_probe


class IntelRuntimeProbeTest(unittest.TestCase):
    def test_accepts_only_fresh_complete_correct_rendering(self):
        valid = {"success": True, "sample_count": 4, "rendered_iterations": 100,
                 "duration_ms": 90000, "errors": []}
        cases = [valid, None, "broken", "[]", "null", {**valid, "success": False},
                 {**valid, "rendered_iterations": 99}, {**valid, "duration_ms": 1000},
                 {**valid, "duration_ms": 89999},
                 {**valid, "sample_count": 1}, {**valid, "errors": ["bad readback"]}]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "results"
                calls = []

                def execute(command, **options):
                    calls.append((command, options))
                    if len(calls) == 2 and payload is not None:
                        (output / "metal-report.json").write_text(
                            payload if isinstance(payload, str) else json.dumps(payload))

                self.assertEqual(intel_runtime_probe.run_probe(output, execute=execute), payload == valid)
                self.assertEqual([options["timeout"] for _, options in calls], [120, 105])
                self.assertTrue(all(options["check"] for _, options in calls))

    def test_process_failures_and_timeouts_are_not_accepted(self):
        for failure in (subprocess.CalledProcessError(1, "control"),
                        subprocess.TimeoutExpired("control", 105)):
            with self.subTest(failure=type(failure)), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "results"

                def execute(*args, **kwargs):
                    raise failure

                self.assertFalse(intel_runtime_probe.run_probe(output, execute=execute))
                self.assertFalse(json.loads((output / "summary.json").read_text())["success"])

    def test_preserves_existing_results_instead_of_reusing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            report = output / "metal-report.json"
            report.write_text("existing evidence")
            with self.assertRaises(FileExistsError):
                intel_runtime_probe.run_probe(output)
            self.assertEqual(report.read_text(), "existing evidence")

    def test_workflow_probe_is_manual_bounded_and_keeps_normal_checks(self):
        path = Path(__file__).resolve().parents[1] / "workflows/workflow_checks.yaml"
        workflow = yaml.safe_load(path.read_text())
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertIs(inputs["intel_runtime_probe"]["default"], False)
        job = workflow["jobs"]["intel-runtime-probe"]
        self.assertEqual(job["if"], "github.event_name == 'workflow_dispatch' && inputs.intel_runtime_probe")
        self.assertEqual(job["strategy"]["matrix"]["os"], ["macos-15-intel", "macos-26-intel"])
        self.assertIs(job["strategy"]["fail-fast"], False)
        self.assertEqual(job["timeout-minutes"], 25)
        self.assertEqual(workflow["jobs"]["workflow-tests"]["runs-on"], "ubuntu-latest")
        docker = next(step for step in job["steps"] if step.get("id") == "docker")
        self.assertEqual(docker["uses"], "./.github/actions/macos-docker")
        self.assertEqual(docker["if"], "matrix.os == 'macos-26-intel'")
        self.assertEqual(docker["timeout-minutes"], 15)


if __name__ == "__main__":
    unittest.main()
