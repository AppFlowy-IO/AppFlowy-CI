"""Exercise real owned child processes, including stalls that keep writing logs."""

import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

import run_mobile_tests as mobile


class MobileTestGuardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mobile-guard-test-")
        self.addCleanup(self.temporary.cleanup)
        self.report = Path(self.temporary.name) / "report.json"
        self.output = io.StringIO()

    def run_child(self, program, build=3, case=3):
        return mobile.run_bounded(
            [sys.executable, "-u", "-c", program], build_seconds=build,
            case_seconds=case, report_path=self.report, output=self.output,
            grace_seconds=0.1,
        )

    def test_successful_cases_are_forwarded_and_case_clock_resets(self):
        result = self.run_child(
            "import time\nprint('00:00 +0: First case')\ntime.sleep(.3)\n"
            "print('00:01 +1: Second case')\ntime.sleep(.3)\n"
            "print('00:02 +2: All tests passed!')\n", case=.5)
        self.assertEqual(result, 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["started_cases"], ["First case", "Second case"])
        self.assertFalse(report["timed_out"])
        self.assertIn("Second case", self.output.getvalue())

    def test_failed_flutter_exit_is_preserved(self):
        self.assertEqual(self.run_child("import sys\nprint('00:00 +0: Failed case')\nsys.exit(7)"), 7)
        self.assertFalse(json.loads(self.report.read_text())["timed_out"])

    def test_build_failure_summary_does_not_create_an_application_case(self):
        result = self.run_child(
            "import sys\nprint('00:00 +0: loading test.dart')\n"
            "print('00:00 +0 -1: Some tests failed.')\nsys.exit(7)\n")
        self.assertEqual(result, 7)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["phase"], "build")
        self.assertIsNone(report["current_case"])
        self.assertEqual(report["started_cases"], [])

    def test_loading_line_uses_separate_build_deadline(self):
        result = self.run_child(
            "import time\nprint('00:00 +0: loading test.dart')\ntime.sleep(60)",
            build=.2, case=3)
        self.assertEqual(result, 124)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["phase"], "build")
        self.assertIsNone(report["current_case"])

    def test_repeated_progress_for_same_case_cannot_extend_its_deadline(self):
        before = time.monotonic()
        result = self.run_child(
            "import time\nwhile True:\n print('00:01 +0: Stalled case')\n time.sleep(.02)",
            case=.2)
        self.assertEqual(result, 124)
        self.assertLess(time.monotonic() - before, 3)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["started_cases"], ["Stalled case"])
        self.assertIn("case deadline", self.output.getvalue())

    def test_case_stall_is_stopped_even_after_stdout_closes(self):
        result = self.run_child(
            "import os,time\nprint('00:00 +0: Closed output')\n"
            "os.close(1)\nos.close(2)\ntime.sleep(60)", case=.2)
        self.assertEqual(result, 124)
        self.assertEqual(json.loads(self.report.read_text())["phase"], "case")

    def test_partial_output_cannot_block_the_watchdog_reader(self):
        result = self.run_child(
            "import sys,time\nsys.stdout.write('partial line')\n"
            "sys.stdout.flush()\ntime.sleep(60)", build=.2)
        self.assertEqual(result, 124)
        self.assertIn("partial line", self.output.getvalue())

    def test_timeout_diagnostics_observe_the_child_before_termination(self):
        child_pid = Path(self.temporary.name) / "child.pid"
        observed_live_child = []

        def capture_timeout():
            os.kill(int(child_pid.read_text()), 0)
            observed_live_child.append(True)

        result = mobile.run_bounded(
            [sys.executable, "-u", "-c",
             "import os,time\nfrom pathlib import Path\n"
             f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
             "print('00:00 +0: Stalled case')\ntime.sleep(60)"],
            build_seconds=3, case_seconds=.2, report_path=self.report,
            output=self.output, on_timeout=capture_timeout, grace_seconds=.1,
        )
        self.assertEqual(result, 124)
        self.assertEqual(observed_live_child, [True])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(child_pid.read_text()), 0)
        self.assertTrue(json.loads(self.report.read_text())["timed_out"])

    def test_timeout_report_survives_interrupted_diagnostics(self):
        child_pid = Path(self.temporary.name) / "child.pid"

        def interrupt_capture():
            os.kill(int(child_pid.read_text()), 0)
            signal.raise_signal(signal.SIGINT)

        with self.assertRaises(KeyboardInterrupt):
            mobile.run_bounded(
                [sys.executable, "-u", "-c",
                 "import os,time\nfrom pathlib import Path\n"
                 f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
                 "print('00:00 +0: Interrupted capture')\ntime.sleep(60)"],
                build_seconds=3, case_seconds=.2, report_path=self.report,
                output=self.output, on_timeout=interrupt_capture, grace_seconds=.1,
            )
        self.assertTrue(self.report.is_file(),
                        "The active case must be saved before interruptible diagnostics")
        report = json.loads(self.report.read_text())
        self.assertTrue(report["timed_out"])
        self.assertEqual(report["current_case"], "Interrupted capture")
        self.assertEqual(report["started_cases"], ["Interrupted capture"])
        self.assertEqual(report["report_stage"], "timeout_detected")
        self.assertIsNone(report["returncode"])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(child_pid.read_text()), 0)

    def test_timeout_also_stops_a_helper_that_ignores_parent_signals(self):
        heartbeat = Path(self.temporary.name) / "heartbeat"
        helper_pid = Path(self.temporary.name) / "helper.pid"
        helper = (
            "import os,signal,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"Path({str(helper_pid)!r}).write_text(str(os.getpid()))\n"
            f"p=Path({str(heartbeat)!r})\n"
            "while True:\n p.write_text(str(time.monotonic()))\n time.sleep(.02)\n"
        )

        def cleanup_helper():
            if helper_pid.exists():
                try:
                    os.kill(int(helper_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(cleanup_helper)
        result = self.run_child(
            "import subprocess,sys,time\nfrom pathlib import Path\n"
            f"subprocess.Popen([sys.executable, '-u', '-c', {helper!r}])\n"
            f"while not Path({str(heartbeat)!r}).exists(): time.sleep(.01)\n"
            "print('00:00 +0: Helper stall')\ntime.sleep(60)", case=.3)
        self.assertEqual(result, 124)
        last_beat = heartbeat.read_text()
        time.sleep(.15)
        self.assertEqual(heartbeat.read_text(), last_beat)

    def test_existing_report_is_not_overwritten(self):
        self.report.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.run_child("raise SystemExit('must not run')")
        self.assertEqual(self.report.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
