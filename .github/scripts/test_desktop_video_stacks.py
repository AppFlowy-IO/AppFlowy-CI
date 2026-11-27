"""Exercise the video watchdog with real Linux processes and dummy data."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from desktop_video_stacks import stack_sample, watch


@unittest.skipUnless(sys.platform == "linux" and shutil.which("cc"), "requires Linux and cc")
class VideoStacksTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.executable = directory / "bundle/AppFlowy"
        self.executable.parent.mkdir()
        self.report = directory / "stacks.json"
        source = directory / "fixture.c"
        source.write_text(r"""
#include <linux/prctl.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <unistd.h>
static int park_fixture(const char *argument) {
    volatile const char *local = "PRIVATE_LOCAL_VALUE";
    printf("%d\n", getpid());
    fflush(stdout);
    for (;;) pause();
}
int main(int argc, char **argv) {
    if (argv[1][0] == 'y') prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY);
    else prctl(PR_SET_DUMPABLE, 0);
    prctl(PR_SET_NAME, "PRIVATE_THREAD");
    return park_fixture(argv[2]);
}
""")
        subprocess.run(["cc", "-g", "-O0", "-o", self.executable, source], check=True)

    def start(self, executable, allow_attach=True):
        # The step shell/Flutter launcher is an ancestor, not the app itself.
        launcher = subprocess.Popen(
            [sys.executable, "-c", "import subprocess,sys; subprocess.run(sys.argv[1:])",
             executable, "yes" if allow_attach else "no", "PRIVATE_ARGUMENT_VALUE"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
            env={**os.environ, "VIDEO_FIXTURE_SECRET": "PRIVATE_ENVIRONMENT_VALUE"},
        )
        self.addCleanup(self.stop, launcher)
        return launcher, int(launcher.stdout.readline())

    @staticmethod
    def stop(launcher):
        if launcher.poll() is None:
            os.killpg(launcher.pid, signal.SIGTERM)
        launcher.wait(timeout=3)
        launcher.stdout.close()

    def test_symbols_only_for_native_descendant_and_no_unrelated_process(self):
        if not Path("/usr/bin/gdb").is_file():
            self.skipTest("requires GDB for a real native backtrace")
        _, decoy = self.start(self.executable)
        launcher, target = self.start(self.executable)
        watch(launcher.pid, self.executable, self.report, delay=0)
        receipt = self.report.read_text()
        result = json.loads(receipt)["apps"][0]
        self.assertEqual(result["pid"], target)
        self.assertNotEqual(result["pid"], decoy)
        self.assertEqual(result["status"], "captured")
        frames = next(t for t in result["threads"] if t["tid"] == target)["frames"]
        self.assertTrue(any(f["symbol"] == "park_fixture" and f["module"] == "AppFlowy"
                            and f["file_offset"].startswith("0x") for f in frames), frames)
        self.assertNotIn("PRIVATE_", receipt)
        self.assertNotIn(str(self.executable.parent), receipt)
        status = Path(f"/proc/{target}/status").read_text()
        self.assertRegex(status, r"(?m)^TracerPid:\s*0$")
        self.assertNotRegex(status, r"(?m)^State:\s*[TtZ]")
        self.assertIsNone(launcher.poll())

    def test_healthy_step_exit_returns_without_capture_or_changing_status(self):
        with subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(.08); raise SystemExit(17)"]) as step:
            before = time.monotonic()
            watch(step.pid, self.executable, self.report, delay=60)
            self.assertLess(time.monotonic() - before, 5)
            self.assertEqual(step.wait(), 17)
            self.assertFalse(self.report.exists())

    def test_missing_app_and_debugger_leave_bounded_fallback(self):
        decoy = self.executable.parent.parent / "other/AppFlowy"
        decoy.parent.mkdir()
        shutil.copy(self.executable, decoy)
        unrelated, _ = self.start(decoy)
        watch(unrelated.pid, self.executable, self.report, delay=0)
        self.assertEqual(json.loads(self.report.read_text()), {"matching_processes": 0, "apps": []})
        launcher, pid = self.start(self.executable)
        watch(launcher.pid, self.executable, self.report, delay=0, debugger=Path("/no/gdb"))
        result = json.loads(self.report.read_text())["apps"][0]
        self.assertEqual(result["status"], "gdb unavailable")
        self.assertIn(pid, [thread["tid"] for thread in result["wait_states"]])
        self.assertFalse(result["threads"])

    def test_restricted_sibling_ptrace_leaves_target_running(self):
        scope = Path("/proc/sys/kernel/yama/ptrace_scope")
        if not Path("/usr/bin/gdb").is_file() or os.geteuid() == 0 or not scope.exists() or scope.read_text().strip() == "0":
            self.skipTest("requires restricted Linux ptrace without root")
        launcher, target = self.start(self.executable, allow_attach=False)
        result = stack_sample(target, self.executable, Path("/usr/bin/gdb"), False)
        self.assertEqual(result["status"], "attach unavailable")
        self.assertEqual(result["threads"], [])
        self.assertIsNone(launcher.poll())

    def test_hung_debugger_is_bounded_without_stopping_the_app(self):
        launcher, pid = self.start(self.executable)
        debugger = self.report.parent / "stalled-debugger"
        debugger.write_text("#!/bin/sh\nexec /bin/sleep 60\n")
        debugger.chmod(0o755)
        with patch("desktop_video_stacks.DEBUGGER_SECONDS", 0.15):
            before = time.monotonic()
            result = stack_sample(pid, self.executable, debugger, False)
            self.assertLess(time.monotonic() - before, 5)
        self.assertEqual(result, {"status": "debugger timed out", "threads": []})
        self.assertIsNone(launcher.poll())

    @unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true"
                         and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
                         and Path("/usr/bin/gdb").is_file(),
                         "requires the GitHub-hosted Ubuntu debugger")
    def test_hosted_runner_elevated_attachment_to_test_owned_child(self):
        _, target = self.start(self.executable, allow_attach=False)
        result = stack_sample(target, self.executable, Path("/usr/bin/gdb"), True)
        self.assertEqual(result["status"], "captured")
        self.assertTrue(any(f["symbol"] == "park_fixture"
                            for thread in result["threads"] for f in thread["frames"]))
        print("Verified bounded sudo/GDB attachment to the fixture on the hosted runner.")


class VideoWorkflowTest(unittest.TestCase):
    def test_only_video_shards_start_and_upload_a_sample_before_the_original_timeout(self):
        path = Path(__file__).resolve().parents[1] / "workflows/flutter_ci.yaml"
        job = yaml.safe_load(path.read_text())["jobs"]["integration_test"]
        steps = {step.get("name"): step for step in job["steps"]}
        run = steps["Run Flutter integration tests"]
        self.assertEqual(run["timeout-minutes"], 45)
        self.assertIn('15|26)', run["run"])
        self.assertIn('"$$" "$PWD/build/linux/x64/debug/bundle/AppFlowy"', run["run"])
        self.assertLess(run["run"].index("desktop_video_stacks.py"), run["run"].index("flutter pub get"))
        self.assertIn("TEST_EXIT=${PIPESTATUS[0]}", run["run"])
        self.assertTrue(steps["Ensure GDB is available for video timeout diagnostics"]["continue-on-error"])
        self.assertTrue(steps["Upload native video stacks if sampled"]["continue-on-error"])
        self.assertEqual(steps["Upload native video stacks if sampled"]["if"],
                         "always() && (matrix.test_number == 15 || matrix.test_number == 26)")

    def test_healthy_shard_shell_preserves_flutter_exit_and_finishes_without_a_report(self):
        script_dir = Path(__file__).resolve().parent
        workflow = yaml.safe_load((script_dir.parent / "workflows/flutter_ci.yaml").read_text())
        step = next(s for s in workflow["jobs"]["integration_test"]["steps"]
                    if s.get("name") == "Run Flutter integration tests")
        for code in (0, 23):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "frontend/appflowy_flutter").mkdir(parents=True)
                (root / ".desktop-ci-tools/.github").mkdir(parents=True)
                (root / ".desktop-ci-tools/.github/scripts").symlink_to(script_dir)
                for name in ("sudo", "dart", "flutter"):
                    stub = root / name
                    stub.write_text("#!/bin/sh\n" +
                                    ('if [ "$1" = test ]; then exit "$FIXTURE_STATUS"; fi\n'
                                     if name == "flutter" else "") + "exit 0\n")
                    stub.chmod(0o755)
                script = step["run"].replace("${{ matrix.test_number }}", "26")
                script = script.replace("/tmp/test_output.txt", str(root / "test.log"))
                script = script.replace("/tmp/desktop-test-events.jsonl", str(root / "events.jsonl"))
                result = subprocess.run(["bash", "-eo", "pipefail", "-c", script],
                                        cwd=root, capture_output=True, timeout=5,
                                        env={**os.environ, "PATH": f"{root}:" + os.environ["PATH"],
                                             "FIXTURE_STATUS": str(code), "GITHUB_WORKSPACE": tmp,
                                             "RUNNER_TEMP": tmp})
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertFalse((root / "desktop-video-native-stacks.json").exists())
