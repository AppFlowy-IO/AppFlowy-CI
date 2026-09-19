"""Flutter runner checks: python3 -B -m unittest discover -s .github/scripts.

Requires PyYAML. Executes the actual workflow shell snippets with a stub Flutter
command; no app, cloud services, or GitHub credentials are used.
"""

import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/flutter_ci.yaml"
MANIFEST = Path("frontend/appflowy_flutter/integration_test/desktop/cloud/ci_suites.json")
SPLIT_SUITES = [
    "workspace",
    "sharing",
    "space_permissions",
    "sidebar",
    "database",
    "document",
]
LEGACY_SUITES = ["core_workspace", "sidebar", "database", "document"]


class DesktopRunnerWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        cls.prepare = jobs["prepare-linux"]
        cls.desktop = jobs["integration_test"]
        cls.selection = next(
            step
            for step in cls.prepare["steps"]
            if step.get("id") == "desktop-test-runners"
        )

    def select_runners(self, files=(), directories=()):
        with tempfile.TemporaryDirectory(prefix="desktop-ci-matrix-") as directory:
            root = Path(directory)
            tests = root / "frontend/appflowy_flutter/integration_test"
            tests.mkdir(parents=True)
            for name in files:
                target = tests / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            for name in directories:
                (tests / name).mkdir()
            output = root / "github-output"
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", self.selection["run"]],
                cwd=root,
                env={**os.environ, "GITHUB_OUTPUT": str(output)},
                capture_output=True,
                text=True,
                timeout=5,
            )
            contents = output.read_text() if output.exists() else ""
            return result, contents

    def test_new_and_old_revisions_schedule_their_existing_runners(self):
        for count in [9, 15, 16, 17]:
            with self.subTest(count=count):
                numbers = list(range(1, count + 1))
                result, output = self.select_runners(
                    [f"desktop_runner_{number}.dart" for number in reversed(numbers)]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(output.removeprefix("runners=")), numbers)

    def test_only_top_level_numeric_runner_files_are_scheduled(self):
        result, output = self.select_runners(
            files=[
                "desktop_runner_17.dart", "desktop_runner_2.dart",
                "desktop_runner_10.dart", "desktop_runner_cloud.dart",
                "desktop_runner_0.dart", "desktop_runner_02.dart",
                "desktop_runner_7.dart.bak", "mobile_runner_1.dart",
                "nested/desktop_runner_3.dart",
            ],
            directories=["desktop_runner_4.dart"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.removeprefix("runners=")), [2, 10, 17])

    def test_missing_runners_fail_instead_of_omitting_tests(self):
        result, output = self.select_runners()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.assertIn("No desktop integration test runners found", result.stderr)

    def test_matrix_consumes_preparation_output(self):
        self.assertEqual(
            self.prepare["outputs"]["desktop_test_runners"],
            "${{ steps.desktop-test-runners.outputs.runners }}",
        )
        self.assertEqual(
            self.desktop["strategy"]["matrix"]["test_number"],
            "${{ fromJSON(needs.prepare-linux.outputs.desktop_test_runners) }}",
        )
        self.assertIn("prepare-linux", self.desktop["needs"])
        self.assertFalse(self.desktop["strategy"]["fail-fast"])


class CloudRunnerWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        cls.prepare = cls.jobs["prepare-linux"]
        cls.cloud = cls.jobs["cloud_integration_test"]
        cls.selection = next(
            step
            for step in cls.prepare["steps"]
            if step.get("id") == "cloud-test-suites"
        )
        cls.test_step = next(
            step
            for step in cls.cloud["steps"]
            if step.get("name", "").startswith("Run Flutter integration tests")
        )

    def select_suites(self, manifest=None):
        with tempfile.TemporaryDirectory(prefix="cloud-ci-matrix-") as directory:
            root = Path(directory)
            if manifest is not None:
                target = root / MANIFEST
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps(manifest))
            output = root / "github-output"
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", self.selection["run"]],
                cwd=root,
                env={**os.environ, "GITHUB_OUTPUT": str(output)},
                capture_output=True,
                text=True,
                timeout=5,
            )
            contents = output.read_text() if output.exists() else ""
            return result, contents

    def test_new_revisions_use_split_matrix(self):
        result, output = self.select_suites(SPLIT_SUITES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.removeprefix("suites=")), SPLIT_SUITES)

    def test_old_revisions_keep_legacy_matrix(self):
        result, output = self.select_suites()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.removeprefix("suites=")), LEGACY_SUITES)

    def test_invalid_manifest_fails_instead_of_omitting_tests(self):
        for manifest in [
            [], {}, "workspace", [1], ["workspace", "workspace"], ["bad;name"],
        ]:
            with self.subTest(manifest=manifest):
                result, output = self.select_suites(manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")

    def test_matrix_consumes_preparation_output(self):
        self.assertEqual(
            self.prepare["outputs"]["cloud_test_suites"],
            "${{ steps.cloud-test-suites.outputs.suites }}",
        )
        self.assertEqual(
            self.cloud["strategy"]["matrix"]["cloud_test_suite"],
            "${{ fromJSON(needs.prepare-linux.outputs.cloud_test_suites) }}",
        )
        self.assertIn("prepare-linux", self.cloud["needs"])
        self.assertFalse(self.cloud["strategy"]["fail-fast"])

    def test_logging_keeps_flutter_exit_status(self):
        for code, output in [
            (0, "✅ example"),
            (1, "❌ example (failed)"),
            (17, "Compiler failed before registering tests"),
        ]:
            with self.subTest(exit_code=code), tempfile.TemporaryDirectory() as directory:
                script = self.execution_script(directory)
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script],
                    env={**os.environ, "STUB_EXIT": str(code), "STUB_OUTPUT": output},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(output, result.stdout)
                self.assertEqual(
                    (Path(directory) / "test_output.txt").read_text(), output + "\n",
                )

    def test_real_failure_is_not_masked_by_cleanup_exception(self):
        output = (
            "🎉 1 tests passed.\n"
            "unhandled error during finalization of test\n"
            "PathNotFoundException: Deletion failed flutter_test_listener\n"
            "❌ example (failed)"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    "bash", "-e", "-o", "pipefail", "-c",
                    self.execution_script(directory),
                ],
                env={**os.environ, "STUB_EXIT": "1", "STUB_OUTPUT": output},
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1, result.stderr)

    def test_runner_receives_the_https_cloud_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = Path(directory) / "flutter-arguments"
            script = (
                'flutter() { printf "%s\\n" "$@" > "$FLUTTER_ARGUMENTS"; }\n'
                + self.execution_script(directory, include_stub=False)
            )
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                env={
                    **os.environ,
                    **self.cloud["env"],
                    "FLUTTER_ARGUMENTS": str(arguments),
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "--dart-define=APPFLOWY_CLOUD_URL=https://localhost",
                arguments.read_text().splitlines(),
            )

    def test_output_is_visible_before_flutter_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = 'flutter() { echo "test has started"; read -r release; }\n'
            script = stub + self.execution_script(directory, include_stub=False)
            process = subprocess.Popen(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                readable, _, _ = select.select([process.stdout], [], [], 3)
                self.assertTrue(
                    readable, "Test progress stayed buffered until command completion",
                )
                self.assertEqual(process.stdout.readline().strip(), "test has started")
                self.assertIsNone(process.poll())
            finally:
                process.communicate(input="finish\n", timeout=5)
            self.assertEqual(process.returncode, 0)

    def test_timeout_leaves_budget_for_partial_log_uploads(self):
        self.assertEqual(
            self.cloud["timeout-minutes"],
            "${{ matrix.cloud_test_suite == 'timeline' && 90 || 60 }}",
        )
        self.assertEqual(self.test_step["timeout-minutes"], 45)
        steps = self.cloud["steps"]
        upload = next(
            step for step in steps
            if step.get("name") == "Upload cloud test output"
        )
        self.assertEqual(upload["if"], "always()")
        self.assertIn("/tmp/test_output.txt", upload["with"]["path"])
        self.assertIn("/tmp/cloud-test-events.jsonl", upload["with"]["path"])
        self.assertIn(
            "--file-reporter=json:/tmp/cloud-test-events.jsonl", self.test_step["run"],
        )
        self.assertLess(steps.index(self.test_step), steps.index(upload))
        for name in ["Collect Docker logs", "Upload Docker logs"]:
            step = next(step for step in steps if step.get("name") == name)
            self.assertEqual(step["if"], "failure() || cancelled()")

    def test_only_timeline_uses_the_locally_built_self_hosted_server(self):
        setup = next(
            step for step in self.cloud["steps"]
            if step.get("name") == "Run Docker-Compose"
        )
        for suite in ["timeline", "database"]:
            with self.subTest(suite=suite), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                commands = root / "docker-commands"
                # Run the actual suite-selection script without touching Docker
                # or removing the machine's MinIO directory.
                script = (
                    'docker() { printf "%s\\n" "$*" >> "$DOCKER_COMMANDS"; }\n'
                    'sudo() { :; }\n'
                    + setup["run"]
                )
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script],
                    cwd=root,
                    env={
                        **os.environ,
                        "CLOUD_TEST_SUITE": suite,
                        "GITHUB_RUN_ID": "123",
                        "DOCKER_COMMANDS": str(commands),
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                compose = "compose -f docker-compose-ci.yml"
                override = root / "docker-compose.timeline.yml"
                if suite == "timeline":
                    compose += " -f docker-compose.timeline.yml"
                    services = yaml.safe_load(override.read_text())["services"]
                    self.assertEqual(set(services), {"appflowy_cloud"})
                    cloud = services["appflowy_cloud"]
                    self.assertEqual(cloud["image"], "appflowy-timeline-ci:${GITHUB_RUN_ID}")
                    self.assertEqual(cloud["pull_policy"], "never")
                    self.assertEqual(
                        cloud["environment"]["APPFLOWY_COMMERCIAL_FREE_MAX_USERS"],
                        "10000",
                    )
                else:
                    self.assertFalse(override.exists())
                executed = commands.read_text().splitlines()
                self.assertIn(f"{compose} pull", executed)
                self.assertIn(f"{compose} up -d", executed)

    def execution_script(self, directory, include_stub=True):
        # Exercise only test execution/reporting, not service setup or pub get.
        script = "set +e\n" + self.test_step["run"].split("set +e\n", 1)[1]
        script = script.replace("${{ matrix.cloud_test_suite }}", "workspace")
        script = script.replace("/tmp/test_output.txt", f"{directory}/test_output.txt")
        script = script.replace("/tmp/cloud-test-events.jsonl", f"{directory}/events.jsonl")
        if include_stub:
            stub = 'flutter() { printf "%s\\n" "$STUB_OUTPUT"; return "$STUB_EXIT"; }\n'
            script = stub + script
        return script


if __name__ == "__main__":
    unittest.main()
