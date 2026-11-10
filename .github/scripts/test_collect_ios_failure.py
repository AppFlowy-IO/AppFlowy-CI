import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import collect_ios_failure as diagnostics


DEVICE = "76A49098-D9BB-4A4C-B49F-AD9AD7ECBEC7"


class IosDiagnosticsTest(unittest.TestCase):
    def test_unavailable_simulator_commands_leave_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def unavailable(command, **kwargs):
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            with patch.object(diagnostics.subprocess, "run", side_effect=unavailable):
                diagnostics.collect(DEVICE, root / "output", reports=root / "reports")
            report = json.loads((root / "output/summary.json").read_text())
            self.assertEqual(len(report["commands"]), 3)
            self.assertTrue(all("error" in command for command in report["commands"]))
            self.assertIn("Diagnostic command unavailable", (root / "output/screenshot.log").read_text())

    def test_samples_only_selected_simulator_app_and_copies_only_recent_app_crashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            for name in ("AppFlowy-recent.ips", "Runner-old.crash", "Unrelated.ips"):
                (reports / name).write_text(name)
            os.utime(reports / "Runner-old.crash", (1, 1))
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                self.assertEqual(kwargs["timeout"], 40 if command[0] == "sample" else 20)
                if command[0] == "ps":
                    kwargs["stdout"].write("111 /unrelated/Runner.app/Runner\n"
                                           f"222 /CoreSimulator/{DEVICE}/AppFlowy.app/AppFlowy\n")
                return subprocess.CompletedProcess(command, 0)
            with patch.object(diagnostics.subprocess, "run", side_effect=run):
                diagnostics.collect(DEVICE, root / "output", reports=reports)
            self.assertTrue((root / "output/AppFlowy-recent.ips").exists())
            self.assertFalse((root / "output/Runner-old.crash").exists())
            self.assertFalse((root / "output/Unrelated.ips").exists())
            self.assertEqual(next(command for command in calls if command[0] == "sample")[1], "222")

    def test_rejects_invalid_device_before_writing_or_running_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(diagnostics.subprocess, "run") as run:
            output = Path(directory) / "output"
            with self.assertRaises(ValueError):
                diagnostics.collect("not-a-device", output)
            self.assertFalse(output.exists())
            run.assert_not_called()
