"""Selection and effective server settings must agree before a special fixture runs."""

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

import cloud_fixture_modes as modes


def inventory(mode):
    names = [mode.test] if mode.exact else [f"{mode.test}case_{i}" for i in range(mode.minimum)]
    return "".join(f"{name}: test\n" for name in names)


def configuration(mode):
    return {"services": {
        service: {"environment": {
            "APPFLOWY_DATABASE_HISTORY_ENABLED": str(mode.history).lower(),
            "APPFLOWY_MAX_DASHBOARD_WIDGETS": str(mode.view_limit),
        }} for service in mode.services
    }}


class FixtureModesTest(unittest.TestCase):
    def test_inventory_refuses_empty_ignored_or_unrelated_selections(self):
        for mode in modes.MODES.values():
            modes.verify_inventory(mode, inventory(mode), "0 tests, 0 benchmarks\n")
            for selected, ignored in [("", ""), (inventory(mode), inventory(mode)),
                                      ("wrong::case: test\n" * mode.minimum, "")]:
                with self.subTest(mode=mode.test, selected=selected, ignored=ignored):
                    with self.assertRaises(RuntimeError):
                        modes.verify_inventory(mode, selected, ignored)

    def test_legacy_mode_requires_both_writers_to_use_the_larger_cap(self):
        mode = modes.MODES["legacy-duplication"]
        config = configuration(mode)
        modes.verify_compose(mode, config)
        config["services"]["appflowy_worker"]["environment"]["APPFLOWY_MAX_DASHBOARD_WIDGETS"] = "6"
        with self.assertRaisesRegex(RuntimeError, "appflowy_worker"):
            modes.verify_compose(mode, config)

    def test_effective_history_mismatch_fails_before_any_restart(self):
        mode = modes.MODES["database-websocket-loss"]
        config = configuration(mode)
        config["services"]["appflowy_cloud"]["environment"]["APPFLOWY_DATABASE_HISTORY_ENABLED"] = "true"
        with patch.object(modes.subprocess, "check_output", side_effect=[
            inventory(mode), "", json.dumps(config),
        ]), patch.object(modes.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "database-history"):
                modes.run_mode(mode, "sync-v2,ci-test")
            run.assert_not_called()

    def test_override_renders_old_and_configurable_sources_without_changing_other_services(self):
        for history in ["true", "${APPFLOWY_CI_DATABASE_HISTORY_ENABLED:-true}"]:
            for mode in modes.MODES.values():
                with self.subTest(history=history, mode=mode.test), TemporaryDirectory() as directory:
                    source = Path(directory) / "docker-compose-ci.yml"
                    source.write_text(json.dumps({"services": {
                        "appflowy_cloud": {"image": "busybox", "environment": {
                            "APPFLOWY_DATABASE_HISTORY_ENABLED": history,
                            "APPFLOWY_MAX_DASHBOARD_WIDGETS": "${APPFLOWY_MAX_DASHBOARD_WIDGETS:-6}",
                            "PRESERVED": "cloud",
                        }},
                        "appflowy_worker": {"image": "busybox", "environment": {
                            "APPFLOWY_MAX_DASHBOARD_WIDGETS": "${APPFLOWY_MAX_DASHBOARD_WIDGETS:-6}",
                            "PRESERVED": "worker",
                        }},
                        "redis": {"image": "redis:7", "environment": {"PRESERVED": "redis"}},
                    }}))
                    original = source.read_bytes()
                    environment = {**os.environ, "APPFLOWY_MAX_DASHBOARD_WIDGETS": "3",
                                   "APPFLOWY_CI_DATABASE_HISTORY_ENABLED": str(not mode.history).lower()}
                    baseline = json.loads(subprocess.check_output(
                        ["docker", "compose", "-f", str(source), "config", "--format", "json"],
                        env=environment, text=True,
                    ))
                    with modes.fixture_compose(mode) as compose:
                        override = Path(compose[-1])
                        self.assertEqual(set(json.loads(override.read_text())["services"]),
                                         set(mode.services))
                        rendered = json.loads(subprocess.check_output(
                            [*compose, "config", "--format", "json"],
                            cwd=directory, env=environment, text=True,
                        ))
                        modes.verify_compose(mode, rendered)
                        for service, settings in baseline["services"].items():
                            if service not in mode.services:
                                self.assertEqual(rendered["services"][service], settings)
                            self.assertEqual(rendered["services"][service]["environment"]["PRESERVED"],
                                             settings["environment"]["PRESERVED"])
                    self.assertFalse(override.exists(), "the phase override must be temporary")
                    self.assertEqual(source.read_bytes(), original)

    def test_server_readiness_requires_the_advertised_capability_and_limit(self):
        mode = modes.MODES["database-websocket-loss"]
        replies = [
            {"code": 0, "data": {"enable_database_history": True, "max_dashboard_widgets": 6}},
            {"code": 0, "data": {"enable_database_history": False, "max_dashboard_widgets": 10}},
            {"code": 0, "data": {"enable_database_history": False, "max_dashboard_widgets": 6}},
        ]
        with patch.object(modes, "urlopen", side_effect=[
            io.BytesIO(json.dumps(reply).encode()) for reply in replies
        ]) as request, patch.object(modes.time, "sleep"):
            modes.wait_for_server(mode, "http://localhost")
        self.assertEqual(request.call_count, 3)
        headers = dict(request.call_args.args[0].header_items())
        self.assertEqual(headers["Client-version"], "0.14.6")
        self.assertEqual(headers["X-platform"], "app")

    def test_readiness_timeout_prevents_the_test_run(self):
        mode = modes.MODES["database-websocket-loss"]
        with patch.object(modes.time, "monotonic", side_effect=[0, 121]):
            with self.assertRaisesRegex(RuntimeError, "did not enter"):
                modes.wait_for_server(mode, "http://localhost")
        with self.prepared(mode), patch.object(modes.subprocess, "run") as run:
            with patch.object(modes, "wait_for_server", side_effect=RuntimeError("unready")):
                with self.assertRaisesRegex(RuntimeError, "unready"):
                    modes.run_mode(mode, "sync-v2,ci-test")
            self.assertEqual(run.call_count, 1, "only the service restart can run")

    @contextmanager
    def prepared(self, mode):
        with patch.object(modes.subprocess, "check_output", side_effect=[
            inventory(mode), "", json.dumps(configuration(mode)),
        ]):
            yield

    def test_modes_reuse_the_binary_and_only_restart_the_affected_services(self):
        for mode in modes.MODES.values():
            with self.subTest(mode=mode.test), self.prepared(mode), \
                    patch.object(modes, "wait_for_server"), \
                    patch.object(modes.subprocess, "run") as run:
                modes.run_mode(mode, "sync-v2,ci-test")
                restart, tests = run.call_args_list
                self.assertEqual(restart.args[0][-len(mode.services):], list(mode.services))
                self.assertIn("--no-deps", restart.args[0])
                self.assertIn("--no-build", restart.args[0])
                self.assertIn(mode.test, tests.args[0])
                self.assertEqual("--exact" in tests.args[0], mode.exact)
                self.assertEqual(tests.args[0][-1], "--test-threads=1")
                self.assertTrue(tests.kwargs["check"], "assertion failures must fail the job")
                self.assertEqual(tests.kwargs["env"]["APPFLOWY_MAX_DASHBOARD_WIDGETS"], str(mode.view_limit))
                override = Path(restart.args[0][restart.args[0].index("up") - 1])
                self.assertFalse(override.exists(), "clean up after a successful phase")

    def test_service_restart_failures_prevent_test_execution(self):
        mode = modes.MODES["legacy-duplication"]
        with self.prepared(mode), patch.object(modes.subprocess, "run", side_effect=
                subprocess.CalledProcessError(1, ["docker", "compose"])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                modes.run_mode(mode, "sync-v2,ci-test")
            self.assertEqual(run.call_count, 1)
            override = Path(run.call_args.args[0][run.call_args.args[0].index("up") - 1])
            self.assertFalse(override.exists(), "clean up when the restart fails")

    def test_workflow_reassigns_both_special_selections_after_ordinary_tests(self):
        workflow = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
        steps = yaml.safe_load(workflow.read_text())["jobs"]["test"]["steps"]
        ordinary = next(step for step in steps if step.get("name") == "Run Tests")
        deletion = next(step for step in steps if step.get("name") == "Final seeded account deletion")
        for name, service in [("database-websocket-loss", "appflowy_cloud_collab"),
                              ("legacy-duplication", "appflowy_cloud_workspace_content")]:
            mode = modes.MODES[name]
            special = next(step for step in steps
                           if f"cloud_fixture_modes.py {name}" in step.get("run", ""))
            self.assertIn(f"--skip {mode.test}", ordinary["run"])
            self.assertIn(f"matrix.test_service == '{service}'", special["if"])
            self.assertLess(steps.index(ordinary), steps.index(special))
            self.assertLess(steps.index(special), steps.index(deletion))
            self.assertNotIn("continue-on-error", special)


if __name__ == "__main__":
    unittest.main()
