"""Exercise private simulator configuration and the existing four-job contract."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import mobile_row_cloud


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ios_ci.yaml"


class MobileCloudConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mobile-cloud-config-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cloud = self.root / "cloud"
        self.desktop = self.root / "desktop"
        self.cloud.mkdir()
        self.app = self.desktop / "frontend/appflowy_flutter"
        target = self.app / mobile_row_cloud.ROW_PAGE_TEST
        target.parent.mkdir(parents=True)
        target.write_text("void main() {}\n")
        self.config = self.app / "integration_test/shared/cloud_test_config.dart"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("APPFLOWY_TEST_GOTRUE_ADMIN_EMAIL\nAPPFLOWY_TEST_GOTRUE_ADMIN_PASSWORD\n")
        (self.cloud / "deploy.env").write_text(
            "# Existing example configuration\nPOSTGRES_DB=postgres\nDERIVED_URL=${SCHEME}://${FQDN}\n"
            "GOTRUE_ADMIN_EMAIL=example@example.com\nGOTRUE_ADMIN_PASSWORD=example\n"
        )

    def prepare(self):
        return mobile_row_cloud.prepare(self.cloud, self.desktop, self.root)

    def test_simulator_and_cloud_share_private_credentials_without_printing_them(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), contextlib.redirect_stdout(output):
            defines = self.prepare()
        values = json.loads(defines.read_text())
        env = (self.cloud / ".env").read_text()
        for suffix in ("EMAIL", "PASSWORD"):
            value = values[f"APPFLOWY_TEST_GOTRUE_ADMIN_{suffix}"]
            self.assertIn(f"GOTRUE_ADMIN_{suffix}='{value}'\n", env)
            self.assertEqual(env.count(f"GOTRUE_ADMIN_{suffix}="), 1)
        self.assertEqual(values["APPFLOWY_CLOUD_URL"], "http://localhost")
        self.assertEqual(values["APPFLOWY_TEST_GOTRUE_URL"], "http://localhost/gotrue")
        self.assertEqual(values["APPFLOWY_CLOUD_FORCE_SELF_HOST"], "true")
        self.assertIn("POSTGRES_DB=postgres\n", env)
        self.assertLess(env.index("FQDN="), env.index("DERIVED_URL="))
        self.assertLess(env.index("SCHEME="), env.index("DERIVED_URL="))
        self.assertNotIn("GOTRUE_ADMIN_EMAIL=example", env)
        self.assertEqual(output.getvalue(), "")
        for path in (defines, self.cloud / ".env"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_files_are_never_overwritten(self):
        for path in (self.cloud / ".env", self.root / "mobile-row-cloud-defines.json"):
            with self.subTest(path=path.name):
                path.write_text("existing configuration")
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual(path.read_text(), "existing configuration")
                path.unlink()

    def test_missing_runner_or_credential_support_fails_before_writing(self):
        target = self.app / mobile_row_cloud.ROW_PAGE_TEST
        target.unlink()
        with self.assertRaisesRegex(ValueError, "no mobile row-page tests"):
            self.prepare()
        target.touch()
        self.config.write_text("APPFLOWY_TEST_GOTRUE_ADMIN_EMAIL")
        with self.assertRaisesRegex(ValueError, "lacks simulator credential defines"):
            self.prepare()
        self.assertFalse((self.cloud / ".env").exists())
        self.assertFalse((self.root / "mobile-row-cloud-defines.json").exists())

    def test_partial_write_failure_removes_only_new_configuration_files(self):
        fdopen = os.fdopen
        for fail_on in (1, 2):
            calls = []

            @contextlib.contextmanager
            def failing_fdopen(descriptor, mode):
                calls.append(descriptor)
                with fdopen(descriptor, mode) as file:
                    if len(calls) != fail_on:
                        yield file
                        return

                    class PartialWriter:
                        def write(self, content):
                            file.write(content[:10])
                            file.flush()
                            raise OSError("simulated disk failure after partial write")

                    yield PartialWriter()

            with self.subTest(fail_on=fail_on), patch.object(os, "fdopen", failing_fdopen):
                with self.assertRaises(OSError):
                    self.prepare()
            self.assertFalse((self.cloud / ".env").exists())
            self.assertFalse((self.root / "mobile-row-cloud-defines.json").exists())
            self.assertTrue((self.cloud / "deploy.env").exists())


class MobileCloudWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.job = cls.workflow["jobs"]["integration-tests"]
        cls.steps = cls.job["steps"]

    def step(self, name):
        return next(step for step in self.steps if step.get("name") == name)

    def test_cloud_reuses_runner_four_after_the_existing_local_suite(self):
        self.assertEqual(set(self.workflow["jobs"]), {"integration-tests", "docker-setup-smoke"})
        self.assertEqual(self.job["if"],
                         "github.event_name != 'workflow_dispatch' || inputs.validation_scope != 'docker_setup'")
        self.assertEqual(self.job["strategy"]["matrix"], {"test_number":
            "${{ fromJSON(github.event_name == 'workflow_dispatch' && inputs.test_runner == '4' "
            "&& '[4]' || '[1,2,3,4]') }}"})
        inputs = self.workflow[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["test_runner"]["options"], ["all", "4"])
        self.assertEqual(inputs["test_runner"]["default"], "all")
        self.assertEqual(inputs["test_runner"]["type"], "choice")
        self.assertEqual(self.job["runs-on"],
                         "${{ matrix.test_number == 4 && 'macos-15-intel' || 'macos-latest' }}")
        local = self.step("Run Flutter integration tests (runner ${{ matrix.test_number }})")
        cloud = self.step("Run Cloud row-page tests on iOS")
        setup = self.step("Setup Docker on macOS")
        self.assertNotIn("if", local)
        self.assertLess(self.steps.index(local), self.steps.index(setup))
        self.assertLess(self.steps.index(setup), self.steps.index(cloud))
        for step in self.steps[self.steps.index(local) + 1:]:
            self.assertIn("matrix.test_number == 4", step["if"])
        self.assertIn(mobile_row_cloud.ROW_PAGE_TEST, cloud["run"])
        self.assertIn('--dart-define-from-file="$RUNNER_TEMP/mobile-row-cloud-defines.json"', cloud["run"])
        self.assertNotIn("integration_test/runner.dart", cloud["run"])

    def test_local_test_failure_does_not_skip_cloud_but_setup_failures_do(self):
        discovery = self.step("Discover focused Cloud row-page runner")
        self.assertIn("!cancelled()", discovery["if"])
        self.assertIn("steps.local-mobile-tests.outcome == 'failure'", discovery["if"])
        self.assertIn("steps.local-mobile-tests.outcome == 'success'", discovery["if"])
        tools = self.step("Checkout workflow tools")
        self.assertIn("!cancelled()", tools["if"])
        self.assertIn("steps.cloud-discovery.outputs.enabled == 'true'", tools["if"])
        previous = tools
        for name in (
            "Checkout Cloud fixture", "Prepare private Cloud and simulator configuration",
            "Setup Docker on macOS", "Log in to Docker Hub", "Start the verified exact Cloud runtime",
            "Start job-owned Cloud services",
            "Wait for Cloud readiness", "Run Cloud row-page tests on iOS",
        ):
            step = self.step(name)
            self.assertIn("!cancelled()", step["if"])
            self.assertIn(f"steps.{previous['id']}.outcome == 'success'", step["if"])
            previous = step

    def test_runner_four_builds_for_the_booted_simulator_and_propagates_failure(self):
        simulator = next(step for step in self.steps
                         if step.get("id") == "simulator-action")
        self.assertTrue(simulator["with"].get("wait_for_boot", False))
        prebuild = self.step("Pre-build iOS app for simulator")
        self.assertEqual(prebuild.get("if"), "matrix.test_number != 4")
        local = self.step("Run Flutter integration tests (runner ${{ matrix.test_number }})")
        self.assertNotIn("if", local)
        self.assertNotIn("--no-build", local["run"])
        command = local["run"].replace("${{ matrix.test_number }}", "4").replace(
            "${{ steps.simulator-action.outputs.udid }}", "owned-simulator-id")
        with tempfile.TemporaryDirectory(prefix="selected-ios-build-") as directory:
            root = Path(directory)
            calls = root / "calls"
            flutter = root / "flutter"
            flutter.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$XCODE_XCCONFIG_FILE" "$@" > "$CALLS"\nexit 7\n')
            flutter.chmod(0o700)
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", command], cwd=root,
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "IOS_RUST_TARGET": "x86_64-apple-ios", "RUNNER_TEMP": directory,
                     "CALLS": str(calls)},
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 7)
            self.assertEqual(calls.read_text().splitlines(), [
                str(root / "mobile-intel-simulator.xcconfig"),
                "test", "integration_test/mobile_runner_4.dart", "-d", "owned-simulator-id",
            ])

    def test_only_revisions_with_the_focused_entrypoint_select_cloud(self):
        discovery = self.step("Discover focused Cloud row-page runner")["run"]
        for supported in (False, True):
            with self.subTest(supported=supported), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if supported:
                    target = root / "frontend/appflowy_flutter" / mobile_row_cloud.ROW_PAGE_TEST
                    target.parent.mkdir(parents=True)
                    target.touch()
                output = root / "output"
                summary = root / "summary"
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", discovery], cwd=root,
                    env={**os.environ, "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                         "CLOUD_SOURCE_SHA": "", "CLOUD_IMAGE_RUN_ID": ""},
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(), f"enabled={str(supported).lower()}\n")
                if supported:
                    self.assertFalse(summary.exists())
                else:
                    self.assertIn("not applicable", summary.read_text())

    def test_exact_request_cannot_silently_skip_an_older_desktop(self):
        discovery = self.step("Discover focused Cloud row-page runner")["run"]
        for source, image in (("a" * 40, "123"), ("a" * 40, ""), ("", "123")):
            with self.subTest(source=source, image=image), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", discovery], cwd=root,
                    env={**os.environ, "GITHUB_OUTPUT": str(output),
                         "GITHUB_STEP_SUMMARY": str(root / "summary"),
                         "CLOUD_SOURCE_SHA": source, "CLOUD_IMAGE_RUN_ID": image},
                    capture_output=True, text=True, timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Exact Cloud validation requires", result.stdout)
                self.assertFalse(output.exists())

    def test_exact_runtime_reuses_the_source_verified_composite(self):
        inputs = self.workflow[True]["workflow_dispatch"]["inputs"]
        for name in ("cloud_source_sha", "cloud_image_run_id"):
            self.assertEqual(inputs[name]["type"], "string")
            self.assertFalse(inputs[name]["required"])
        exact = self.step("Start the verified exact Cloud runtime")
        self.assertEqual(exact["uses"], "./.mobile-ci-tools/.github/actions/desktop-cloud-runtime")
        self.assertEqual(exact["with"]["cloud-source-sha"], "${{ env.CLOUD_SOURCE_SHA }}")
        self.assertEqual(exact["with"]["image-run-id"], "${{ env.CLOUD_IMAGE_RUN_ID }}")
        self.assertEqual(exact["with"]["desktop-ref"],
                         "${{ github.event.client_payload.pr_ref || inputs.pr_ref || 'main' }}")
        self.assertEqual(exact["with"]["cloud-path"], ".mobile-cloud")
        self.assertEqual(self.step("Checkout Cloud fixture")["with"]["ref"],
                         "${{ github.event.client_payload.cloud_source_sha || inputs.cloud_source_sha || 'main' }}")
        self.assertEqual(self.step("Checkout workflow tools")["with"]["ref"], "${{ github.sha }}")
        self.assertIn("steps.exact-cloud.outcome == 'success'",
                      self.step("Start job-owned Cloud services")["if"])
        self.assertIn("desktop-cloud-manifest.json", self.step("Upload Cloud image identities")["with"]["path"])
        self.assertNotIn("workflow run", WORKFLOW.read_text())

    def test_exact_runtime_never_pulls_registry_services_or_falls_back_on_invalid_output(self):
        script = self.step("Start job-owned Cloud services")["run"]
        for enabled in ("true", "false", "", "unexpected"):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls = root / "calls"
                prefix = 'docker() { echo "$*" >> "$CALLS"; }\ngit() { echo "fixture-sha"; }\n'
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", prefix + script], cwd=root,
                    env={**os.environ, "EXACT_CLOUD_ENABLED": enabled,
                         "RUNNER_TEMP": directory, "CALLS": str(calls)},
                    capture_output=True, text=True, timeout=5,
                )
                if enabled not in ("true", "false"):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(calls.exists())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = calls.read_text().splitlines()
                expected = ["compose -f docker-compose-ci.yml images --format json"]
                if enabled == "false":
                    expected = ["compose -f docker-compose-ci.yml pull",
                                "compose -f docker-compose-ci.yml up -d --no-build", *expected]
                self.assertEqual(commands, expected)

    def test_intel_rust_target_profile_and_simulator_match(self):
        env = self.job["env"]
        for value in env.values():
            self.assertNotIn("runner.", value, "runner context is unavailable in job.env")
        self.assertIn('echo "MINIO_DATA_DIR=$RUNNER_TEMP/mobile-row-cloud-minio" >> "$GITHUB_ENV"',
                      self.step("Prepare private Cloud and simulator configuration")["run"])
        self.assertIn("matrix.test_number == 4 && 'x86_64-apple-ios'", env["IOS_RUST_TARGET"])
        self.assertIn("matrix.test_number == 4 && 'development-ios-x86_64-sim'", env["IOS_BUILD_PROFILE"])
        toolchain = self.step("Install Rust toolchain")
        self.assertEqual(toolchain["with"]["target"], "${{ env.IOS_RUST_TARGET }}")
        self.assertIn('rustup target install "$IOS_RUST_TARGET"', self.step("Install prerequisites")["run"])
        build = self.step("Build AppFlowy")["run"]
        self.assertEqual(build.count('cargo make --profile "$IOS_BUILD_PROFILE"'), 2)
        self.assertIn('test "$(uname -m)" = x86_64', self.step("Verify Docker runner architecture")["run"])

    def test_intel_simulator_override_requires_matching_ffi_and_preserves_desktop_config(self):
        prepare = self.step("Prepare Intel simulator architecture")
        self.assertEqual(prepare["if"], "matrix.test_number == 4")
        self.assertLess(self.steps.index(self.step("Build AppFlowy")), self.steps.index(prepare))
        self.assertLess(self.steps.index(prepare), self.steps.index(self.step("Pre-build iOS app for simulator")))
        for architecture in ("x86_64", "arm64", "x86_64 arm64"):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                temporary = root / "job temporary files"
                temporary.mkdir()
                config = root / "frontend/appflowy_flutter/ios/Flutter/Debug.xcconfig"
                config.parent.mkdir(parents=True)
                original = "EXCLUDED_ARCHS[sdk=iphonesimulator*] = x86_64\n"
                config.write_text(original)
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c",
                     'lipo() { printf "%s\\n" "$FFI_ARCHS"; }\n' + prepare["run"]],
                    cwd=root, env={**os.environ, "RUNNER_TEMP": str(temporary), "FFI_ARCHS": architecture},
                    capture_output=True, text=True, timeout=5,
                )
                override = temporary / "mobile-intel-simulator.xcconfig"
                self.assertEqual(config.read_text(), original)
                if architecture != "x86_64":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(override.exists())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(override.read_text().splitlines(), [
                    "ARCHS[sdk=iphonesimulator*] = x86_64",
                    "EXCLUDED_ARCHS[sdk=iphonesimulator*] = i386 arm64",
                ], "The override must not affect macOS host tools or physical-device builds")
                self.assertNotIn("GITHUB_ENV", prepare["run"])

    def test_only_flutter_simulator_commands_receive_the_intel_override(self):
        commands = {
            "Pre-build iOS app for simulator": ["build", "ios", "--simulator", "--debug"],
            "Run Flutter integration tests (runner ${{ matrix.test_number }})":
                ["test", "integration_test/mobile_runner_4.dart", "-d", "fixture-device"],
            "Run Cloud row-page tests on iOS":
                ["test", "integration_test/mobile_row_page_cloud_runner.dart", "-d", "fixture-device"],
        }
        self.assertNotIn("XCODE_XCCONFIG_FILE", self.workflow.get("env", {}))
        self.assertNotIn("XCODE_XCCONFIG_FILE", self.job["env"])
        for step in self.steps:
            if step.get("name") not in commands:
                self.assertNotIn("XCODE_XCCONFIG_FILE", step.get("run", ""))
                self.assertNotIn("XCODE_XCCONFIG_FILE", step.get("env", {}))
        for target in ("x86_64-apple-ios", "aarch64-apple-ios-sim"):
            for name, arguments in commands.items():
                with self.subTest(target=target, step=name), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    flutter = root / "flutter"
                    flutter.write_text(
                        '#!/usr/bin/env python3\nimport json, os, sys\n'
                        'from pathlib import Path\n'
                        'Path(os.environ["CAPTURE"]).write_text(json.dumps({\n'
                        ' "args": sys.argv[1:], "override": os.getenv("XCODE_XCCONFIG_FILE")}))\n'
                    )
                    flutter.chmod(0o755)
                    script = self.step(name)["run"].replace("${{ matrix.test_number }}", "4").replace(
                        "${{ steps.simulator-action.outputs.udid }}", "fixture-device")
                    env = {key: value for key, value in os.environ.items() if key != "XCODE_XCCONFIG_FILE"}
                    result = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                        env={**env, "PATH": f"{root}:{env['PATH']}", "RUNNER_TEMP": directory,
                             "CAPTURE": str(root / "capture.json"), "IOS_RUST_TARGET": target},
                        capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    captured = json.loads((root / "capture.json").read_text())
                    self.assertEqual(captured["args"][:len(arguments)], arguments)
                    if target == "x86_64-apple-ios":
                        self.assertEqual(captured["override"], str(root / "mobile-intel-simulator.xcconfig"))
                    else:
                        self.assertIsNone(captured["override"])

    def test_checkouts_cannot_clean_away_each_other(self):
        desktop = self.step("Checkout private repository")
        for name in ("Checkout workflow tools", "Checkout Cloud fixture"):
            checkout = self.step(name)
            self.assertLess(self.steps.index(desktop), self.steps.index(checkout))
            self.assertTrue(checkout["with"]["path"].startswith(".mobile-"))
        self.assertEqual(self.step("Checkout workflow tools")["with"]["ref"], "${{ github.sha }}")

    def test_latest_images_and_scoped_cleanup_have_no_global_prune(self):
        for variable in ("APPFLOWY_CLOUD_VERSION", "APPFLOWY_WORKER_VERSION", "APPFLOWY_HISTORY_VERSION"):
            self.assertEqual(self.job["env"][variable], "latest-amd64")
        self.assertIn("github.run_id", self.job["env"]["COMPOSE_PROJECT_NAME"])
        start = self.step("Start job-owned Cloud services")["run"]
        self.assertIn("pull", start)
        self.assertIn("up -d --no-build", start)
        cleanup = self.step("Remove job-owned Cloud services and credentials")
        self.assertIn("always()", cleanup["if"])
        self.assertIn("steps.cloud-config.outcome == 'success'", cleanup["if"])
        self.assertIn("down --volumes --remove-orphans", cleanup["run"])
        self.assertNotIn("prune", WORKFLOW.read_text())
        artifacts = self.step("Upload Cloud image identities")["with"]["path"]
        self.assertNotIn(".env", artifacts)
        self.assertNotIn("defines.json", artifacts)

    def test_readiness_failure_blocks_test_instead_of_proceeding(self):
        script = self.step("Wait for Cloud readiness")["run"]
        for status in (0, 1):
            with self.subTest(status=status):
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c",
                     'curl() { return "$STUB_STATUS"; }\nsleep() { :; }\n' + script],
                    env={**os.environ, "STUB_STATUS": str(status)},
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, status, result.stderr)
                if status:
                    self.assertIn("did not become ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
