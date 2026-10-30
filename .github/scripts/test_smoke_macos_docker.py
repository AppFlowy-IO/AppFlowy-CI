"""Validate the smoke test's protocol, failure propagation and manual-only scope."""

import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

import smoke_macos_docker as smoke


class DockerSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="docker-smoke-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "smoke"

    def test_real_host_protocol_and_compose_configuration(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            port = listener.getsockname()[1]
            requests = []

            def answer():
                connection, _ = listener.accept()
                with connection:
                    requests.append(connection.recv(64))
                    connection.sendall(b"+PONG\r\n")

            server = threading.Thread(target=answer, daemon=True)
            server.start()
            with patch.object(smoke.subprocess, "run") as run, \
                 patch.object(smoke.subprocess, "check_output", side_effect=[
                     "linux/amd64\n", f"127.0.0.1:{port}\n"]):
                smoke.smoke(self.root, "owned-project")
            server.join(timeout=5)
        self.assertFalse(server.is_alive())
        self.assertEqual(requests, [b"*1\r\n$4\r\nPING\r\n"])
        compose = json.loads((self.root / "compose.json").read_text())
        self.assertEqual(compose["services"]["redis"]["ports"], ["127.0.0.1::6379"])
        self.assertEqual(compose["services"]["redis"]["platform"], "linux/amd64")
        self.assertRegex(compose["services"]["redis"]["image"], r"^redis@sha256:[0-9a-f]{64}$")
        self.assertTrue(all(call.args[0][3] == "owned-project" for call in run.call_args_list))
        self.assertEqual(run.call_args_list[-1].args[0][-3:],
                         ["down", "--volumes", "--remove-orphans"])

    def test_failed_start_is_not_success_and_still_removes_its_services(self):
        error = subprocess.CalledProcessError(7, ["docker", "compose", "up"])
        with patch.object(smoke.subprocess, "run", side_effect=[error, None]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                smoke.smoke(self.root, "owned-project")
        self.assertEqual(run.call_count, 2)
        self.assertIn("down", run.call_args_list[-1].args[0])

    def test_wrong_architecture_or_public_bind_fails_and_cleans_up(self):
        for output in (["linux/arm64\n"], ["linux/amd64\n", "0.0.0.0:6379\n"]):
            with self.subTest(output=output), \
                 patch.object(smoke.subprocess, "run") as run, \
                 patch.object(smoke.subprocess, "check_output", side_effect=output), \
                 patch.object(smoke.socket, "create_connection") as connection:
                root = self.root / str(len(output))
                self.root.mkdir(exist_ok=True)
                with self.assertRaises(ValueError):
                    smoke.smoke(root, "owned-project")
                connection.assert_not_called()
                self.assertIn("down", run.call_args_list[-1].args[0])

    def test_manual_smoke_is_distinct_from_default_four_lane_coverage(self):
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((root / ".github/workflows/ios_ci.yaml").read_text())
        scope = workflow[True]["workflow_dispatch"]["inputs"]["validation_scope"]
        self.assertEqual(scope["default"], "full")
        self.assertEqual(scope["options"], ["full", "docker_setup"])
        job = workflow["jobs"]["docker-setup-smoke"]
        self.assertEqual(job["if"],
                         "github.event_name == 'workflow_dispatch' && inputs.validation_scope == 'docker_setup'")
        self.assertEqual(job["runs-on"], "macos-15-intel")
        self.assertEqual(job["timeout-minutes"], 15)
        self.assertEqual(job["steps"][1]["run"], "python3 .github/scripts/setup_macos_docker.py")
        self.assertEqual(job["steps"][2]["run"], "python3 .github/scripts/smoke_macos_docker.py")
        self.assertEqual(job["steps"][-1]["if"], "always()")
        self.assertIn('"$RUNNER_TEMP/mobile-docker-tools/tools/bin/colima"',
                      job["steps"][-1]["run"])


if __name__ == "__main__":
    unittest.main()
