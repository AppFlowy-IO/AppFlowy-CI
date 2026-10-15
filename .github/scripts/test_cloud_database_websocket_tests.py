"""The WebSocket loss phase must select real tests and change the running server's policy."""

import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

import cloud_database_websocket_tests as websocket


def inventory(names=websocket.REQUIRED_TESTS):
    return "".join(f"{name}: test\n" for name in sorted(names))


def configuration(history="false"):
    return {"services": {"appflowy_cloud": {"environment": {
        "APPFLOWY_DATABASE_HISTORY_ENABLED": history,
    }}}}


class DatabaseWebsocketTests(unittest.TestCase):
    def test_missing_ignored_and_unrelated_tests_fail_before_restarting(self):
        websocket.verify_inventory(inventory(), "0 tests, 0 benchmarks\n")
        missing = websocket.REQUIRED_TESTS - {min(websocket.REQUIRED_TESTS)}
        for selected, ignored in [
            ("", ""), (inventory(missing), ""), (inventory(), inventory()),
            (inventory() + "unrelated::test: test\n", ""),
        ]:
            with self.subTest(selected=selected, ignored=ignored), \
                    patch.object(websocket.subprocess, "check_output", side_effect=[selected, ignored]), \
                    patch.object(websocket.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "missing, ignored or misselected"):
                    websocket.run_tests("sync-v2,ci-test")
                run.assert_not_called()

    def test_override_renders_both_source_forms_without_changing_other_settings(self):
        for history in ["true", "${APPFLOWY_CI_DATABASE_HISTORY_ENABLED:-true}"]:
            with self.subTest(history=history), TemporaryDirectory() as directory:
                source = Path(directory) / "docker-compose-ci.yml"
                source.write_text(json.dumps({"services": {
                    "appflowy_cloud": {"image": "busybox", "environment": [
                        f"APPFLOWY_DATABASE_HISTORY_ENABLED={history}", "PRESERVED=cloud",
                        "APPFLOWY_MAX_DASHBOARD_WIDGETS=6",
                    ]},
                    "appflowy_worker": {"image": "busybox", "environment": {"PRESERVED": "worker"}},
                    "redis": {"image": "redis:7"},
                }}))
                original = source.read_bytes()
                environment = {**os.environ, "APPFLOWY_CI_DATABASE_HISTORY_ENABLED": "true"}
                baseline = json.loads(subprocess.check_output(
                    ["docker", "compose", "-f", str(source), "config", "--format", "json"],
                    env=environment, text=True,
                ))
                with websocket.websocket_compose() as compose:
                    override = Path(compose[-1])
                    rendered = json.loads(subprocess.check_output(
                        [*compose, "config", "--format", "json"],
                        cwd=directory, env=environment, text=True,
                    ))
                    baseline["services"]["appflowy_cloud"]["environment"][
                        "APPFLOWY_DATABASE_HISTORY_ENABLED"
                    ] = "false"
                    self.assertEqual(rendered, baseline)
                self.assertFalse(override.exists())
                self.assertEqual(source.read_bytes(), original)

    def test_wrong_rendered_setting_prevents_restart(self):
        for history in ["true", None]:
            with self.subTest(history=history), \
                    patch.object(websocket.subprocess, "check_output", side_effect=[
                        inventory(), "", json.dumps(configuration(history)),
                    ]), patch.object(websocket.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "must disable database history"):
                    websocket.run_tests("sync-v2,ci-test")
                run.assert_not_called()

    def test_live_policy_requires_success_and_native_client_headers(self):
        replies = [
            {"code": 0, "data": {"enable_database_history": True}},
            {"code": 0, "data": {}},
            {"code": 1, "data": {"enable_database_history": False}},
            {"code": 0, "data": {"enable_database_history": False}},
        ]
        with patch.object(websocket, "urlopen", side_effect=[
            io.BytesIO(json.dumps(reply).encode()) for reply in replies
        ]) as request, patch.object(websocket.time, "sleep"):
            websocket.wait_for_server("https://localhost/")
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args.args[0].full_url, "https://localhost/api/server-info")
        headers = dict(request.call_args.args[0].header_items())
        self.assertEqual(headers["Client-version"], "0.14.6")
        self.assertEqual(headers["X-platform"], "app")

    def test_readiness_timeout_prevents_test_execution(self):
        with patch.object(websocket.time, "monotonic", side_effect=[0, 121]):
            with self.assertRaisesRegex(RuntimeError, "did not disable"):
                websocket.wait_for_server("http://localhost")
        with patch.object(websocket.subprocess, "check_output", side_effect=[
            inventory(), "", json.dumps(configuration()),
        ]), patch.object(websocket.subprocess, "run") as run, \
                patch.object(websocket, "wait_for_server", side_effect=RuntimeError("unready")):
            with self.assertRaisesRegex(RuntimeError, "unready"):
                websocket.run_tests("sync-v2,ci-test")
            self.assertEqual(run.call_count, 1, "only the service restart can run")

    def test_reuses_archive_and_restarts_only_cloud_before_running_tests(self):
        with patch.object(websocket.subprocess, "check_output", side_effect=[
            inventory(), "", json.dumps(configuration()),
        ]), patch.object(websocket, "wait_for_server") as ready, \
                patch.object(websocket.subprocess, "run") as run:
            websocket.run_tests("ai-test-enabled,sync-v2,ci-test")
        restart, tests = run.call_args_list
        ready.assert_called_once()
        self.assertEqual(restart.args[0][-1], "appflowy_cloud")
        self.assertIn("--no-deps", restart.args[0])
        self.assertIn("--no-build", restart.args[0])
        self.assertIn("--pull=never", restart.args[0])
        self.assertEqual(tests.args[0][1], str(Path(websocket.__file__).with_name("cloud_test_binaries.py")))
        self.assertIn(websocket.TEST_PREFIX, tests.args[0])
        self.assertIn("ai-test-enabled,sync-v2,ci-test", tests.args[0])
        self.assertEqual(tests.args[0][-1], "--test-threads=1")
        self.assertEqual(restart.kwargs["env"]["APPFLOWY_CLOUD_VERSION"], "latest-amd64")
        override = Path(restart.args[0][restart.args[0].index("up") - 1])
        self.assertFalse(override.exists(), "clean up after the phase succeeds")

    def test_restart_and_test_failures_fail_the_phase_and_clean_up(self):
        for fail_at in [0, 1]:
            outcomes = [None] * fail_at + [subprocess.CalledProcessError(42, ["fixture"])]
            with self.subTest(fail_at=fail_at), \
                    patch.object(websocket.subprocess, "check_output", side_effect=[
                        inventory(), "", json.dumps(configuration()),
                    ]), patch.object(websocket, "wait_for_server"), \
                    patch.object(websocket.subprocess, "run", side_effect=outcomes) as run:
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    websocket.run_tests("sync-v2,ci-test")
                self.assertEqual(failure.exception.returncode, 42)
                self.assertEqual(run.call_count, fail_at + 1)
                for call in run.call_args_list:
                    self.assertTrue(call.kwargs["check"])
                restart = run.call_args_list[0].args[0]
                self.assertFalse(Path(restart[restart.index("up") - 1]).exists())

    def test_phase_runs_after_ordinary_tests_before_account_deletion(self):
        workflow = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
        steps = yaml.safe_load(workflow.read_text())["jobs"]["test"]["steps"]
        ordinary = next(step for step in steps if step.get("name") == "Run Tests")
        special = next(step for step in steps if step.get("name") == "Database WebSocket loss regressions")
        deletion = next(step for step in steps if step.get("name") == "Final seeded account deletion")
        self.assertIn("matrix.test_service == 'appflowy_cloud_collab'", special["if"])
        self.assertIn("hashFiles('tests/collab/missing_user_updates/database.rs') != ''", special["if"])
        self.assertIn("ci-tools/.github/scripts/cloud_database_websocket_tests.py", special["run"])
        self.assertIn('--features "$CLOUD_TEST_FEATURES"', special["run"])
        self.assertLess(steps.index(ordinary), steps.index(special))
        self.assertLess(steps.index(special), steps.index(deletion))
        self.assertNotIn("continue-on-error", special)


if __name__ == "__main__":
    unittest.main()
