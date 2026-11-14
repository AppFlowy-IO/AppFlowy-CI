"""Exercise bounded package downloads without root access or network requests."""

import os
from pathlib import Path
import subprocess
import unittest

from run_flutter_apt import run_apt


class FlutterAptDownloadTest(unittest.TestCase):
    def invoke(self, codes, arguments=("install", "-y", "libmpv-dev", "mpv")):
        calls = []
        results = iter(codes)

        def run(command):
            calls.append(command)
            return subprocess.CompletedProcess(command, next(results))

        return run_apt(list(arguments), run), calls

    def test_timed_out_download_resumes_before_installation(self):
        status, calls = self.invoke([124, 0, 0])
        self.assertEqual(status, 0)
        self.assertEqual(calls[0], calls[1])
        self.assertIn("120s", calls[0])
        self.assertIn("--download-only", calls[0])
        self.assertEqual(calls[2], ["sudo", "apt-get", "--no-download", "install", "-y", "libmpv-dev", "mpv"])
        self.assertNotIn("timeout", calls[2])

    def test_repeated_timeout_never_installs_incomplete_archives(self):
        status, calls = self.invoke([124, 124])
        self.assertEqual(status, 124)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("--download-only" in call for call in calls))

    def test_package_errors_are_not_retried(self):
        status, calls = self.invoke([100])
        self.assertEqual(status, 100)
        self.assertEqual(len(calls), 1)

    def test_install_accepts_options_before_the_command(self):
        status, calls = self.invoke([0, 0], ("-y", "install", "--fix-broken"))
        self.assertEqual(status, 0)
        self.assertIn("--download-only", calls[0])
        self.assertEqual(calls[1][-3:], ["-y", "install", "--fix-broken"])

    def test_installation_failure_remains_a_failure(self):
        status, calls = self.invoke([0, 17])
        self.assertEqual(status, 17)
        self.assertEqual(len(calls), 2)

    def test_index_update_has_the_same_timeout_without_installation(self):
        status, calls = self.invoke([124, 0], ("update",))
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[-2:] == ["apt-get", "update"] for call in calls))

    def test_local_development_environment_is_rejected(self):
        script = Path(__file__).with_name("run_flutter_apt.py")
        result = subprocess.run(
            ["python3", "-B", str(script), "install", "-y", "libmpv-dev"],
            env={**os.environ, "GITHUB_ACTIONS": "false", "RUNNER_OS": "Linux"},
            capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only for GitHub Linux runners", result.stderr)
