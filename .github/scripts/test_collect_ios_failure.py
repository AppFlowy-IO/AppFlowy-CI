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

    def test_samples_only_selected_pending_launch_when_app_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command == ["ps", "-axo", "pid=,comm="]:
                    kwargs["stdout"].write("111 /usr/bin/simctl\n222 /usr/bin/simctl\n333 /usr/bin/dart\n")
                if command == ["ps", "-axo", "pid=,args="]:
                    self.assertEqual(kwargs["timeout"], 3)
                    return subprocess.CompletedProcess(command, 0, stdout=(
                        "111 /usr/bin/simctl launch OTHER-DEVICE com.appflowy.appflowy.flutter\n"
                        f"222 /usr/bin/simctl launch {DEVICE} com.appflowy.appflowy.flutter --enable-checked-mode\n"
                        f"333 /usr/bin/dart launch {DEVICE} com.appflowy.appflowy.flutter\n"))
                return subprocess.CompletedProcess(command, 0)

            with patch.object(diagnostics.subprocess, "run", side_effect=run):
                diagnostics.collect(DEVICE, root / "output", reports=root / "reports")
            samples = [command for command in calls if command[0] == "sample"]
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0][1:3], ["222", "1"])
            self.assertEqual(Path(samples[0][-1]).name, "launch-sample.txt")
            report = json.loads((root / "output/summary.json").read_text())
            self.assertEqual(report["commands"][-1]["command"], samples[0])

    def test_unavailable_launch_lookup_retains_other_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(command, **kwargs):
                if command == ["ps", "-axo", "pid=,comm="]:
                    kwargs["stdout"].write("222 /usr/bin/simctl\n")
                if command == ["ps", "-axo", "pid=,args="]:
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                return subprocess.CompletedProcess(command, 0)

            with patch.object(diagnostics.subprocess, "run", side_effect=run):
                diagnostics.collect(DEVICE, root / "output", reports=root / "reports")
            report = json.loads((root / "output/summary.json").read_text())
            self.assertEqual(len(report["commands"]), 4)
            self.assertEqual(report["commands"][-1]["command"], ["ps", "-axo", "pid=,args="])
            self.assertIn("error", report["commands"][-1])
            self.assertTrue((root / "output/screenshot.log").exists())

    def test_rejects_invalid_device_before_writing_or_running_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(diagnostics.subprocess, "run") as run:
            output = Path(directory) / "output"
            with self.assertRaises(ValueError):
                diagnostics.collect("not-a-device", output)
            self.assertFalse(output.exists())
            run.assert_not_called()
