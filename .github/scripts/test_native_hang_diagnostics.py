"""Exercise native stack selection and idle detection without attaching a debugger."""

import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import native_hang_diagnostics as diagnostics


class NativeHangDiagnosticsTest(unittest.TestCase):
    def test_process_selection_requires_the_job_application_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app_root = root / "build/linux"
            app = app_root / "x64/debug/bundle/AppFlowy"
            app.parent.mkdir(parents=True)
            app.touch()
            other = root / "other/AppFlowy"
            other.parent.mkdir()
            other.touch()
            proc = root / "proc"
            for pid, executable in ((101, app), (102, other), (103, root / "missing")):
                process = proc / str(pid)
                process.mkdir(parents=True)
                (process / "exe").symlink_to(executable)
            (proc / "self").mkdir()
            self.assertEqual(diagnostics.find_test_apps(app_root, proc), [101])

    def test_gdb_capture_disables_arguments_and_keeps_a_bounded_deadline(self):
        def write_trace(*args, **kwargs):
            kwargs["stdout"].write("Thread 1\n#0 native_function\n")
            return Mock(returncode=0)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(diagnostics.shutil, "which", return_value="/usr/bin/gdb"), \
                patch.object(diagnostics, "is_test_app", return_value=True), \
                patch.object(diagnostics.subprocess, "run", side_effect=write_trace) as run, \
                patch.object(diagnostics.os, "kill") as kill:
            result = diagnostics.capture_stack(101, Path("/owned/build"), Path(directory))
            self.assertEqual(result["status"], "captured")
            command = run.call_args.args[0]
            self.assertIn("set print frame-arguments none", command)
            self.assertIn("set auto-load off", command)
            self.assertIn("thread apply all bt 30", command)
            self.assertEqual(command[command.index("-p") + 1], "101")
            self.assertIn("--kill-after=5s", command)
            self.assertEqual(run.call_args.kwargs["timeout"], 25)
            kill.assert_not_called()

    def test_empty_debugger_output_does_not_report_a_captured_stack(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(diagnostics.shutil, "which", return_value="gdb"), \
                patch.object(diagnostics, "is_test_app", return_value=True), \
                patch.object(diagnostics.subprocess, "run", return_value=Mock(returncode=0)):
            result = diagnostics.capture_stack(101, Path("/owned/build"), Path(directory))
            self.assertEqual(result["status"], "debugger_failed")

    def test_a_replaced_process_is_never_attached(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(diagnostics.shutil, "which", return_value="gdb"), \
                patch.object(diagnostics, "is_test_app", return_value=False), \
                patch.object(diagnostics.subprocess, "run") as run:
            result = diagnostics.capture_stack(101, Path("/owned/build"), Path(directory))
            self.assertEqual(result["status"], "process_gone")
            run.assert_not_called()

    def test_missing_debugger_is_reported_without_running_commands(self):
        with patch.object(diagnostics.shutil, "which", return_value=None), \
                patch.object(diagnostics.subprocess, "run") as run:
            result = diagnostics.capture_stack(101, Path("/owned/build"), Path("unused"))
            self.assertEqual(result["status"], "debugger_unavailable")
            run.assert_not_called()

    def test_debugger_timeout_releases_only_the_still_owned_application(self):
        for still_owned in (False, True):
            with self.subTest(still_owned=still_owned), tempfile.TemporaryDirectory() as directory, \
                    patch.object(diagnostics.shutil, "which", return_value="gdb"), \
                    patch.object(diagnostics, "is_test_app", side_effect=[True, still_owned]), \
                    patch.object(diagnostics.subprocess, "run", side_effect=subprocess.TimeoutExpired("gdb", 25)), \
                    patch.object(diagnostics.os, "kill") as kill:
                result = diagnostics.capture_stack(101, Path("/owned/build"), Path(directory))
                self.assertEqual(result["status"], "capture_timed_out")
                if still_owned:
                    kill.assert_called_once_with(101, signal.SIGCONT)
                else:
                    kill.assert_not_called()

    def test_idle_stream_collects_once_and_keeps_its_manifest(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(diagnostics.time, "monotonic", side_effect=[0, 1, 182]), \
                patch.object(diagnostics, "find_test_apps", return_value=[101]), \
                patch.object(diagnostics, "capture_stack", return_value={"status": "captured"}) as capture:
            root = Path(directory)
            events = root / "events.jsonl"
            events.write_text('{"type":"testStart"}\n')
            stopped = Mock()
            stopped.is_set.return_value = False
            result = diagnostics.monitor(events, root / "build", root / "output", stopped)
            self.assertEqual(result["status"], "captured")
            capture.assert_called_once()
            self.assertEqual(json.loads((root / "output/snapshot.json").read_text()), result)

    def test_a_stream_that_keeps_reporting_restarts_the_idle_deadline(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(diagnostics.time, "monotonic", side_effect=[0, 1, 181]), \
                patch.object(diagnostics, "capture_stack") as capture:
            root = Path(directory)
            events = root / "events.jsonl"
            events.write_text("first\n")
            stopped = Mock()
            stopped.is_set.side_effect = [False, False, True]
            stopped.wait.side_effect = lambda _: events.write_text(events.read_text() + "next\n")
            result = diagnostics.monitor(events, root / "build", root / "output", stopped)
            self.assertEqual(result["status"], "stopped_without_stack")
            capture.assert_not_called()

    def test_normal_completion_requires_no_process_or_debugger(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(diagnostics, "find_test_apps") as find:
            root = Path(directory)
            stopped = threading.Event()
            stopped.set()
            output = root / "output"
            output.mkdir()
            (output / "native-backtrace.txt").write_text("stale stack")
            result = diagnostics.monitor(root / "events", root / "build", output, stopped)
            self.assertEqual(result["status"], "stopped_without_stack")
            self.assertFalse((output / "native-backtrace.txt").exists())
            find.assert_not_called()

    def test_ambiguous_applications_are_reported_without_attaching(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(diagnostics.time, "monotonic", side_effect=[0, 181]), \
                patch.object(diagnostics, "find_test_apps", return_value=[101, 102]), \
                patch.object(diagnostics, "capture_stack") as capture:
            root = Path(directory)
            stopped = Mock()
            stopped.is_set.return_value = False
            result = diagnostics.monitor(root / "events", root / "build", root / "output", stopped)
            self.assertEqual(result["status"], "ambiguous_test_processes")
            capture.assert_not_called()
