"""Execute workflow selection/dispatch snippets without a simulator or credentials."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/ios_ci_test.yaml"


class MobileCloudWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.job = cls.workflow["jobs"]["cloud-integration-test"]
        cls.steps = {step.get("id", step.get("name")): step for step in cls.job["steps"]}

    def test_known_coverage_is_selected_and_missing_or_unknown_targets_fail(self):
        cases = {
            "row_page": "integration_test/mobile/bdd/cloud/mobile_row_page_reopen_test.dart",
            "cloud": "integration_test/mobile/cloud/cloud_runner.dart",
            "all": "integration_test/runner.dart",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            for name, body in {"uname": "echo x86_64", "git": "echo fixture"}.items():
                script = binaries / name
                script.write_text("#!/bin/sh\n" + body + "\n")
                script.chmod(0o755)
            env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
                   "GITHUB_OUTPUT": str(root / "output"), "GITHUB_ENV": str(root / "env"), "RUNNER_TEMP": str(root)}
            for suite, target in cases.items():
                file = root / "frontend/appflowy_flutter" / target
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
                (root / "frontend/rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.98.0"\n')
                result = subprocess.run(["bash", "-c", self.steps["mobile-target"]["run"]],
                                        cwd=root, env={**env, "TEST_SUITE": suite}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"target={target}", (root / "output").read_text())
                file.unlink()
            for suite in ["row_page", "../escape", "$(touch injected)"]:
                result = subprocess.run(["bash", "-c", self.steps["mobile-target"]["run"]],
                                        cwd=root, env={**env, "TEST_SUITE": suite}, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "injected").exists())
            (binaries / "uname").write_text("#!/bin/sh\necho arm64\n")
            result = subprocess.run(["bash", "-c", self.steps["mobile-target"]["run"]],
                                    cwd=root, env={**env, "TEST_SUITE": "row_page"}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_simulator_receives_real_target_and_ci_cloud_defines(self):
        step = self.steps["Run Flutter mobile cloud integration tests"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flutter = root / "flutter"
            flutter.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGUMENTS"\n')
            flutter.chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "ARGUMENTS": str(root / "args"),
                   "TEST_TARGET": "integration_test/mobile/bdd/cloud/mobile_row_page_reopen_test.dart",
                   "SIMULATOR_ID": "fixture-device", "RUNNER_TEMP": str(root)}
            subprocess.run(["bash", "-c", step["run"]], cwd=root, env=env, check=True)
            args = (root / "args").read_text().splitlines()
            self.assertEqual(args[:4], ["test", env["TEST_TARGET"], "-d", "fixture-device"])
            for option in ["--dart-define=CI=true", "--dart-define=APPFLOWY_CLOUD_URL=http://localhost",
                           "--dart-define=APPFLOWY_CLOUD_FORCE_SELF_HOST=true"]:
                self.assertIn(option, args)

    def test_exact_sources_and_intel_build_are_required_without_registry_fallback(self):
        self.assertEqual(self.job["runs-on"], "macos-15-intel")
        self.assertEqual(self.steps["Install Rust toolchain"]["with"]["targets"], "x86_64-apple-ios")
        build = self.steps["Build AppFlowy for the Intel simulator"]["run"]
        self.assertIn("development-ios-x86_64-sim", build)
        self.assertIn("lipo -archs", build)
        self.assertNotIn("arm64", build)
        self.assertEqual(self.steps["Checkout private repository"]["with"]["ref"], "${{ inputs.pr_ref }}")
        self.assertEqual(self.steps["Checkout AppFlowy-Cloud-Premium code"]["with"]["ref"], "${{ inputs.cloud_source_sha }}")
        runtime = self.steps["Start the verified exact Cloud runtime"]["with"]
        self.assertEqual(runtime["image-run-id"], "${{ inputs.cloud_image_run_id }}")
        self.assertEqual(runtime["allow-equivalent-shared-source"], "true")
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])
        self.assertNotIn("Run Docker-Compose", self.steps)
        self.assertIn("exit 1", self.steps["Wait for appflowy cloud to be ready"]["run"])
        self.assertEqual(self.workflow[True]["workflow_dispatch"]["inputs"]["test_suite"]["default"], "all")
        action = yaml.safe_load((WORKFLOW.parents[1] / "actions/desktop-cloud-runtime/action.yml").read_text())
        self.assertEqual(action["inputs"]["allow-equivalent-shared-source"]["default"], "false")
        self.assertIn("mobile-cloud-flutter.log", self.steps["Upload mobile CI provenance and failure logs"]["with"]["path"])


if __name__ == "__main__":
    unittest.main()
