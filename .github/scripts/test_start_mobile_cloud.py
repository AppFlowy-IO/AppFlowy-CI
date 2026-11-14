"""Run the legacy startup boundary against a real, isolated Docker substitute."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import start_mobile_cloud


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MobileCloudStartupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mobile-cloud-startup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cloud = self.root / "cloud"
        self.cloud.mkdir()
        self.output = self.root / "output"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.plan = self.root / "plan.json"
        self.calls = self.root / "calls.jsonl"
        docker = self.bin / "docker"
        docker.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys, time
plan = json.loads(pathlib.Path(os.environ["FAKE_DOCKER_PLAN"]).read_text())
calls = pathlib.Path(os.environ["FAKE_DOCKER_CALLS"])
index = len(calls.read_text().splitlines()) if calls.exists() else 0
with calls.open("a") as file:
    file.write(json.dumps({"args":sys.argv[1:],"parallel":os.environ.get("COMPOSE_PARALLEL_LIMIT"),"cwd":os.getcwd()}) + "\\n")
response = plan[index]
print(response.get("stdout", ""), flush=True)
print(response.get("stderr", ""), file=sys.stderr, flush=True)
if response.get("sleep"):
    time.sleep(response["sleep"])
sys.exit(response.get("exit", 0))
''')
        docker.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FAKE_DOCKER_PLAN": str(self.plan), "FAKE_DOCKER_CALLS": str(self.calls),
        }

    def run_start(self, plan, clock=None, **constants):
        self.plan.write_text(json.dumps(plan))
        capture = io.StringIO()
        with patch.dict(os.environ, self.environment), patch.object(start_mobile_cloud, "sleep") as sleep:
            with contextlib.ExitStack() as stack:
                if clock:
                    # The commands still run as real substitute subprocesses;
                    # only their elapsed budget is simulated, independently of
                    # subprocess's real monotonic timeout clock.
                    command = start_mobile_cloud._command
                    calls = iter(plan)

                    def with_elapsed(*args):
                        result = command(*args)
                        clock.advance(next(calls).get("elapsed", 0))
                        return result

                    stack.enter_context(patch.object(start_mobile_cloud, "_monotonic", clock))
                    stack.enter_context(patch.object(start_mobile_cloud, "_command", with_elapsed))
                    sleep.side_effect = clock.advance
                for name, value in constants.items():
                    stack.enter_context(patch.object(start_mobile_cloud, name, value))
                stack.enter_context(contextlib.redirect_stdout(capture))
                result = start_mobile_cloud.start(self.cloud, self.output)
        return result, capture.getvalue(), sleep

    def report(self):
        return json.loads((self.output / "startup.json").read_text())

    def invocations(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_transient_transfer_failure_then_success_preserves_both_attempts(self):
        result, capture, sleep = self.run_start([
            {"stderr": "error pulling image: unexpected EOF", "exit": 1},
            {"stdout": "Image downloaded"}, {"stdout": "Services started"},
        ])
        self.assertEqual(result, 0)
        report = self.report()
        self.assertEqual(report["stage"], "started")
        self.assertEqual([x["exit_code"] for x in report["pull_attempts"]], [1, 0])
        self.assertTrue(report["pull_attempts"][0]["retryable"])
        self.assertIn("unexpected EOF", (self.output / "pull-1.log").read_text())
        self.assertIn("Image downloaded", (self.output / "pull-2.log").read_text())
        self.assertIn("unexpected EOF", capture)
        sleep.assert_called_once_with(10)
        for invocation in self.invocations():
            self.assertEqual(invocation["parallel"], "2")
            self.assertEqual(Path(invocation["cwd"]).resolve(), self.cloud.resolve())
            self.assertEqual(invocation["args"][:7],
                             ["compose", "--ansi", "never", "--progress", "plain", "-f", "docker-compose-ci.yml"])
        self.assertEqual(self.invocations()[-1]["args"][7:], ["up", "-d", "--no-build", "--pull", "never"])
        self.assertGreater(report["pull_attempts"][0]["timeout_seconds"], 860)
        self.assertLessEqual(report["pull_attempts"][0]["timeout_seconds"], 870)
        self.assertGreater(report["startup"]["timeout_seconds"], 120)
        self.assertLessEqual(report["startup"]["timeout_seconds"], 870)
        self.assertEqual(report["budget_seconds"], 870)

    def test_retry_exhaustion_remains_failed_without_starting_containers(self):
        result, _, sleep = self.run_start([
            {"stderr": "connection reset by peer", "exit": 1},
            {"stderr": "TLS handshake timeout", "exit": 2},
            {"stderr": "unexpected EOF", "exit": 3},
        ])
        self.assertEqual(result, 3)
        self.assertEqual(self.report()["exit_code"], 3)
        self.assertEqual(self.report()["stage"], "pull")
        self.assertIsNone(self.report()["startup"])
        self.assertEqual(len(self.invocations()), 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(10,), (20,)])

    def test_terminal_and_unknown_errors_never_retry_or_start(self):
        for message in [
            "unauthorized: authentication required", "pull access denied: unexpected EOF",
            "manifest unknown", "manifest latest-amd64 not found", "no matching manifest for linux/amd64",
            "image does not exist: unexpected EOF", "TLS handshake certificate rejected",
            "no space left on device", "unknown daemon failure",
            "Error pulling registry.example/timeout:eof: unknown failure",
        ]:
            with self.subTest(message=message):
                self.calls.unlink(missing_ok=True)
                result, _, sleep = self.run_start([{"stderr": message, "exit": 19}])
                self.assertEqual(result, 19)
                self.assertEqual(len(self.invocations()), 1)
                self.assertEqual(self.report()["exit_code"], 19)
                self.assertIsNone(self.report()["startup"])
                sleep.assert_not_called()

    def test_container_start_failure_is_retained_without_retry(self):
        result, capture, sleep = self.run_start([{}, {"stderr": "container start rejected", "exit": 42}])
        self.assertEqual(result, 42)
        self.assertEqual(self.report()["stage"], "start")
        self.assertEqual(self.report()["startup"]["exit_code"], 42)
        self.assertIn("container start rejected", (self.output / "up.log").read_text())
        self.assertIn("container start rejected", capture)
        self.assertEqual(len(self.invocations()), 2)
        sleep.assert_not_called()

    def test_total_budget_bounds_a_hung_pull_and_retains_its_output(self):
        result, _, sleep = self.run_start([
            {"stdout": "partial transfer", "sleep": 10},
        ], STARTUP_BUDGET=0.5)
        self.assertEqual(result, 124)
        first = self.report()["pull_attempts"][0]
        self.assertEqual(first["exit_code"], 124)
        self.assertTrue(first["timed_out"])
        self.assertTrue(first["retryable"])
        self.assertIn("partial transfer", (self.output / "pull-1.log").read_text())
        self.assertIn("timed out", (self.output / "pull-1.log").read_text())
        self.assertTrue(self.report()["budget_exhausted"])
        self.assertIsNone(self.report()["startup"])
        self.assertEqual(len(self.invocations()), 1)
        sleep.assert_not_called()

    def test_start_timeout_stays_failed(self):
        result, _, sleep = self.run_start([{}, {"stdout": "starting", "sleep": 10}], STARTUP_BUDGET=0.5)
        self.assertEqual(result, 124)
        self.assertEqual(self.report()["stage"], "start")
        self.assertTrue(self.report()["startup"]["timed_out"])
        self.assertEqual(self.report()["exit_code"], 124)
        sleep.assert_not_called()

    def test_cold_container_start_uses_remaining_total_budget(self):
        result, _, sleep = self.run_start([
            {"elapsed": 30}, {"stdout": "Cold services started", "elapsed": 360},
        ], clock=Clock())
        self.assertEqual(result, 0)
        self.assertEqual(self.report()["stage"], "started")
        self.assertEqual(self.report()["startup"]["timeout_seconds"], 840)
        self.assertFalse(self.report()["budget_exhausted"])
        sleep.assert_not_called()

    def test_cold_image_pull_can_progress_beyond_three_minutes_within_total_budget(self):
        result, _, sleep = self.run_start([
            {"stdout": "Large layer extracted", "elapsed": 360},
            {"elapsed": 180},
        ], clock=Clock())
        self.assertEqual(result, 0)
        report = self.report()
        self.assertGreaterEqual(report["pull_attempts"][0]["timeout_seconds"], 360)
        self.assertEqual(len(report["pull_attempts"]), 1)
        self.assertEqual(report["startup"]["timeout_seconds"], 510)
        self.assertFalse(report["budget_exhausted"])
        sleep.assert_not_called()

    def test_pull_time_and_retry_backoff_reduce_start_budget(self):
        result, _, sleep = self.run_start([
            {"stderr": "unexpected EOF", "exit": 1, "elapsed": 170},
            {"elapsed": 150}, {},
        ], clock=Clock())
        self.assertEqual(result, 0)
        self.assertEqual(self.report()["startup"]["timeout_seconds"], 540)
        self.assertEqual([x["timeout_seconds"] for x in self.report()["pull_attempts"]], [870, 690])
        sleep.assert_called_once_with(10)

    def test_pull_and_backoff_cannot_exceed_remaining_budget(self):
        result, _, sleep = self.run_start([
            {"stderr": "unexpected EOF", "exit": 1, "elapsed": 170},
            {"stderr": "unexpected EOF", "exit": 1, "elapsed": 20},
        ], clock=Clock(), STARTUP_BUDGET=200)
        self.assertEqual(result, 124)
        report = self.report()
        self.assertEqual([x["timeout_seconds"] for x in report["pull_attempts"]], [200, 20])
        self.assertTrue(report["budget_exhausted"])
        self.assertEqual(report["exit_code"], 124)
        self.assertIsNone(report["startup"])
        self.assertEqual(len(self.invocations()), 2)
        sleep.assert_called_once_with(10)

    def test_backoff_is_limited_and_exhausted_budget_does_not_launch_another_command(self):
        result, _, sleep = self.run_start([
            {"stderr": "unexpected EOF", "exit": 1, "elapsed": 180},
        ], clock=Clock(), STARTUP_BUDGET=185)
        self.assertEqual(result, 124)
        sleep.assert_called_once_with(5)
        self.assertEqual(len(self.invocations()), 1)
        self.assertTrue(self.report()["budget_exhausted"])
        self.assertIsNone(self.report()["startup"])

    def test_no_budget_after_pull_preserves_failure_without_starting(self):
        result, _, sleep = self.run_start([{"elapsed": 180}], clock=Clock(), STARTUP_BUDGET=180)
        self.assertEqual(result, 124)
        self.assertEqual(self.report()["stage"], "start")
        self.assertEqual(self.report()["exit_code"], 124)
        self.assertTrue(self.report()["budget_exhausted"])
        self.assertIsNone(self.report()["startup"])
        self.assertEqual(len(self.invocations()), 1)
        sleep.assert_not_called()

    def test_secret_values_are_redacted_before_console_and_artifact_writes(self):
        secret = "fixture-only-secret-1234"
        self.environment["AI_OPENAI_API_KEY"] = secret
        result, capture, _ = self.run_start([
            {"stderr": f"unknown error with {secret}", "exit": 1},
        ])
        self.assertEqual(result, 1)
        self.assertNotIn(secret, capture)
        self.assertIn("[REDACTED]", capture)
        for path in self.output.iterdir():
            self.assertNotIn(secret, path.read_text())

    def test_missing_docker_still_records_failure(self):
        (self.bin / "docker").unlink()
        self.environment["PATH"] = str(self.bin)
        result, _, sleep = self.run_start([])
        self.assertEqual(result, 127)
        self.assertEqual(self.report()["exit_code"], 127)
        self.assertEqual(len(self.report()["pull_attempts"]), 1)
        self.assertIn("Could not run Docker Compose", (self.output / "pull-1.log").read_text())
        sleep.assert_not_called()

    def test_existing_cloud_configuration_is_unchanged_and_never_logged(self):
        private = "GOTRUE_ADMIN_PASSWORD='fixture-only-password'\n"
        (self.cloud / ".env").write_text(private)
        result, capture, _ = self.run_start([{}, {}])
        self.assertEqual(result, 0)
        self.assertEqual((self.cloud / ".env").read_text(), private)
        self.assertEqual(list(self.cloud.iterdir()), [self.cloud / ".env"])
        self.assertNotIn(private, capture)
        for path in self.output.iterdir():
            self.assertNotIn(private, path.read_text())

    def test_cli_returns_nonzero_and_emits_report_before_flutter(self):
        self.plan.write_text(json.dumps([{"stderr": "manifest unknown", "exit": 17}]))
        result = subprocess.run([
            sys.executable, str(Path(start_mobile_cloud.__file__)), "--cloud", str(self.cloud), "--output", str(self.output),
        ], env=dict(os.environ, **self.environment), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(self.report()["stage"], "pull")
        self.assertEqual(self.report()["exit_code"], 17)
        self.assertIsNone(self.report()["startup"])


if __name__ == "__main__":
    unittest.main()
