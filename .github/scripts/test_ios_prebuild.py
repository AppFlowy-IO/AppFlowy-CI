"""Exercise Intel prebuild orchestration without building an app or a simulator."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
PREBUILD = "Pre-build focused iOS app for selected simulator"
LOCAL = "Run Flutter integration tests (runner ${{ matrix.test_number }})"


class IntelPrebuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load((ROOT / ".github/workflows/ios_ci.yaml").read_text())
        cls.job = cls.workflow["jobs"]["integration-tests"]
        cls.steps = cls.job["steps"]

    def step(self, name):
        matches = [step for step in self.steps if step.get("name") == name]
        self.assertEqual(len(matches), 1, f"Expected one workflow step: {name}")
        return matches[0]

    def run_prebuild(self, exit_code):
        prebuild = self.step(PREBUILD)
        script = prebuild["run"].replace("${{ steps.simulator-action.outputs.udid }}", "owned-simulator-id")
        with tempfile.TemporaryDirectory(prefix="ios-prebuild-") as directory:
            root = Path(directory)
            app = root / "Desktop frontend/appflowy_flutter"
            app.mkdir(parents=True)
            temporary = root / "job temporary files"
            temporary.mkdir()
            config = app / "ios/Flutter/Debug.xcconfig"
            config.parent.mkdir(parents=True)
            original = "EXCLUDED_ARCHS[sdk=iphonesimulator*] = x86_64\n"
            config.write_text(original)
            capture = root / "capture.json"
            flutter = root / "flutter"
            flutter.write_text(
                f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                "Path(os.environ['CAPTURE']).write_text(json.dumps({\n"
                " 'args':sys.argv[1:],'override':os.getenv('XCODE_XCCONFIG_FILE'),'cwd':os.getcwd()}))\n"
                "print('prebuild stdout', flush=True)\n"
                "print('prebuild stderr', file=sys.stderr, flush=True)\n"
                f"sys.exit({exit_code})\n"
            )
            flutter.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", script], cwd=app,
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "RUNNER_TEMP": str(temporary), "CAPTURE": str(capture),
                     "IOS_RUST_TARGET": "x86_64-apple-ios",
                     "XCODE_XCCONFIG_FILE": str(root / "unrelated-inherited.xcconfig")},
                capture_output=True, text=True, timeout=5,
            )
            observed = json.loads(capture.read_text())
            log = (temporary / "mobile-local-results/prebuild.log").read_text()
            self.assertEqual(config.read_text(), original)
            self.assertEqual(Path(observed["cwd"]).resolve(), app.resolve())
            return result, observed, log, str(temporary / "mobile-intel-simulator.xcconfig")

    def test_cold_build_and_functional_budgets_are_separate_and_build_gates_tests(self):
        prebuild = self.step(PREBUILD)
        local = self.step(LOCAL)
        cloud = self.step("Run Cloud row-page tests on iOS")
        simulator = next(step for step in self.steps if step.get("id") == "simulator-action")
        self.assertEqual(self.job["timeout-minutes"], "${{ matrix.test_number == 4 && 210 || 120 }}")
        self.assertEqual(prebuild["timeout-minutes"], 90)
        self.assertEqual(local["timeout-minutes"], 60)
        self.assertEqual(cloud["timeout-minutes"], 20)
        self.assertEqual(prebuild["if"], "matrix.test_number == 4")
        self.assertEqual(self.step("Pre-build iOS app for simulator")["if"], "matrix.test_number != 4")
        self.assertEqual(prebuild["working-directory"], "frontend/appflowy_flutter")
        self.assertEqual(prebuild["shell"], "bash")
        self.assertLess(self.steps.index(simulator), self.steps.index(prebuild))
        self.assertLess(self.steps.index(self.step("Prepare Intel simulator architecture")), self.steps.index(prebuild))
        self.assertLess(self.steps.index(prebuild), self.steps.index(local))
        self.assertNotIn("continue-on-error", prebuild)
        self.assertNotIn("if", local)
        self.assertNotIn("--no-build", local["run"])
        self.assertEqual(self.job["runs-on"],
                         "${{ matrix.test_number == 4 && 'macos-15-intel' || 'macos-latest' }}")
        self.assertEqual(simulator["with"]["model"],
                         "${{ matrix.test_number == 4 && 'iPhone 16 Pro' || 'iPhone 17 Pro' }}")
        upload = self.step("Upload local mobile test results")
        self.assertEqual(upload["if"], "always()")
        self.assertIn("mobile-local-results/", upload["with"]["path"])
        for target in (self.workflow.get("env", {}), self.job["env"], prebuild.get("env", {})):
            self.assertNotIn("XCODE_XCCONFIG_FILE", target)
        self.assertNotIn("GITHUB_ENV", prebuild["run"])
        self.assertNotIn("GITHUB_PATH", prebuild["run"])

    def test_prebuild_targets_the_local_suite_and_selected_device_with_owned_override(self):
        result, observed, log, override = self.run_prebuild(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(observed["args"], [
            "build", "ios", "--simulator", "--debug", "--target",
            "integration_test/mobile_runner_4.dart", "-d", "owned-simulator-id", "--verbose",
        ])
        self.assertEqual(observed["override"], override)
        self.assertEqual(log.splitlines(), ["prebuild stdout", "prebuild stderr"])

    def test_prebuild_keeps_both_output_streams_and_the_build_failure(self):
        result, _, log, _ = self.run_prebuild(7)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(log.splitlines(), ["prebuild stdout", "prebuild stderr"])
        self.assertIn("prebuild stdout", result.stdout)
        self.assertIn("prebuild stderr", result.stdout)

    def run_local(self, target, name_filter, arm_diagnostics=False, runner_number="4"):
        script = self.step(LOCAL)["run"].replace("${{ matrix.test_number }}", runner_number).replace(
            "${{ steps.simulator-action.outputs.udid }}", "owned-simulator-id")
        with tempfile.TemporaryDirectory(prefix="ios-local-selection-") as directory:
            root = Path(directory)
            capture = root / "capture.json"
            summary = root / "summary.md"
            flutter = root / "flutter"
            flutter.write_text(
                f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                "Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
                "print('local output', flush=True)\nsys.exit(7)\n"
            )
            flutter.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", script], cwd=root,
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "RUNNER_TEMP": directory, "CAPTURE": str(capture),
                     "GITHUB_STEP_SUMMARY": str(summary), "IOS_RUST_TARGET": target,
                     "IOS_LOCAL_TEST_NAME": name_filter,
                     "IOS_ARM_LAUNCH_DIAGNOSTICS": str(arm_diagnostics).lower()},
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual((root / "mobile-local-results/flutter.log").read_text(), "local output\n")
            return json.loads(capture.read_text()), summary.read_text() if summary.exists() else ""

    def test_manual_filter_is_one_literal_argument_and_reports_partial_coverage(self):
        name_filter = "Mobile date.*|Mobile grid opens row detail.*"
        arguments, summary = self.run_local("x86_64-apple-ios", name_filter)
        self.assertEqual(arguments, [
            "test", "integration_test/mobile_runner_4.dart", "-d", "owned-simulator-id",
            "--verbose", "--reporter", "expanded", "--name", name_filter,
            "--file-reporter", arguments[-1],
        ])
        self.assertTrue(arguments[-1].endswith("/mobile-local-results/tests.json"))
        self.assertIn("does not cover the full local suite", summary)

    def test_ordinary_and_arm_local_commands_keep_full_coverage(self):
        for target, name_filter in (
            ("x86_64-apple-ios", ""),
            ("aarch64-apple-ios-sim", "Mobile hidden row detail"),
        ):
            with self.subTest(target=target):
                arguments, summary = self.run_local(target, name_filter)
                self.assertNotIn("--name", arguments)
                self.assertEqual(summary, "")
                self.assertEqual("--verbose" in arguments, target == "x86_64-apple-ios")

    def test_local_filter_only_comes_from_explicit_manual_input(self):
        trigger = self.workflow.get("on", self.workflow.get(True))
        inputs = trigger["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["local_test_name"]["default"], "")
        self.assertEqual(inputs["local_test_name"]["type"], "string")
        self.assertEqual(self.step(LOCAL)["env"]["IOS_LOCAL_TEST_NAME"],
                         "${{ github.event_name == 'workflow_dispatch' && inputs.local_test_name || '' }}")

    def test_arm_launch_diagnostics_keep_the_architecture_and_full_suite(self):
        arguments, summary = self.run_local(
            "aarch64-apple-ios-sim", "8476", arm_diagnostics=True, runner_number="3")
        self.assertEqual(arguments[:4], [
            "test", "integration_test/mobile_runner_3.dart", "-d", "owned-simulator-id"])
        self.assertIn("--verbose", arguments)
        self.assertIn("--reporter", arguments)
        self.assertNotIn("--name", arguments)
        self.assertEqual(summary, "")
        self.assertEqual(self.step(LOCAL)["env"]["IOS_ARM_LAUNCH_DIAGNOSTICS"],
                         "${{ matrix.test_number == 3 }}")


if __name__ == "__main__":
    unittest.main()
