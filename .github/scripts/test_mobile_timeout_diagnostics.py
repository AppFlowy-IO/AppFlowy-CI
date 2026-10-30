"""Exercise scoped lifecycle extraction with real sandbox files, not application data."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_mobile_tests as mobile

from mobile_timeout_diagnostics import BUNDLE_ID, MAX_TAIL_BYTES, read_sdk_phases, runner_pid


class MobileTimeoutDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mobile-sdk-evidence-")
        self.addCleanup(self.temporary.cleanup)
        self.container = Path(self.temporary.name)
        self.test_root = self.container / "tmp/appflowy_bdd_paths_owned"
        self.data = self.test_root / "temporary/appflowy_integration_test/scenario"
        self.data.mkdir(parents=True)

    def test_only_fixed_phases_from_test_owned_logs_are_returned(self):
        (self.data / "log.2026-10-31").write_text(
            "10-31 01:02:03 INFO cleanup_sdk: joining background thread token=SECRET\n"
            "10-31 01:02:04 INFO arbitrary account and request message SECRET\n")
        installed = self.container / "Library/Application Support"
        installed.mkdir(parents=True)
        (installed / "log.2026-10-31").write_text("cleanup_sdk: complete")
        report = read_sdk_phases(self.container)
        self.assertEqual(report["files_read"], 1)
        self.assertEqual(report["phases"], [{"phase": "cleanup_sdk: joining background thread",
                                           "local_timestamp": "10-31 01:02:03"}])
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn(str(self.data), json.dumps(report))

    def test_symlinked_logs_and_test_directories_are_not_read(self):
        outside = self.container / "outside"
        outside.mkdir()
        log = outside / "log.2026-10-31"
        log.write_text("cleanup_sdk: complete")
        (self.data / log.name).symlink_to(log)
        (self.container / "tmp/appflowy_bdd_paths_link").symlink_to(self.test_root,
                                                                     target_is_directory=True)
        report = read_sdk_phases(self.container)
        self.assertEqual(report["files_read"], 0)
        self.assertEqual(report["phases"], [])

    def test_large_file_reads_only_the_bounded_tail(self):
        (self.data / "log.2026-10-31").write_text(
            "x" * (MAX_TAIL_BYTES * 3) + "\ncleanup_sdk: complete\n")
        report = read_sdk_phases(self.container)
        self.assertEqual(report["bytes_read"], MAX_TAIL_BYTES)
        self.assertEqual(report["phases"][-1]["phase"], "cleanup_sdk: complete")

    def test_runner_pid_requires_a_unique_selected_app_label(self):
        selected = f"5414 0 UIKitApplication:{BUNDLE_ID}[082f][rb-legacy]"
        others = f"1 0 UIKitApplication:{BUNDLE_ID}.other[xx]\n2 0 unrelated"
        self.assertEqual(runner_pid(others + "\n" + selected), 5414)
        self.assertIsNone(runner_pid(others))
        self.assertIsNone(runner_pid(selected + "\n" + selected.replace("5414", "5415")))

    def test_capture_samples_only_the_selected_runner_and_saves_redacted_sdk_phases(self):
        (self.data / "log.2026-10-31").write_text("cleanup_sdk: joining background thread SECRET")
        diagnostics = self.container / "diagnostics"
        diagnostics.mkdir()
        simulator = "BEB0C3A5-7D46-4D50-B4C6-777F0A79718D"
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[-2:] == ["launchctl", "list"]:
                kwargs["stdout"].write(f"5414 0 UIKitApplication:{BUNDLE_ID}[082f][rb-legacy]\n")

        with patch.object(mobile.subprocess, "run", side_effect=run), \
             patch.object(mobile.subprocess, "check_output", return_value=str(self.container)) as lookup:
            mobile.capture_timeout_diagnostics(simulator, diagnostics)
        self.assertEqual(commands[-1][:5], ["/usr/bin/sample", "5414", "5", "1", "-file"])
        self.assertEqual(lookup.call_args.args[0],
                         ["xcrun", "simctl", "get_app_container", simulator, BUNDLE_ID, "data"])
        report = json.loads((diagnostics / "mobile-local-sdk-phases.json").read_text())
        self.assertEqual(report["phases"][-1]["phase"], "cleanup_sdk: joining background thread")
        self.assertNotIn("SECRET", json.dumps(report))
