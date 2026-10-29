"""Exercise the failed-test rerun wrapper against a scripted stand-in for cargo test."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

import retry_failed_tests as retry


# Prints the libtest output scripted for this attempt, records its arguments, exits as scripted.
FAKE_TEST_COMMAND = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    directory = Path(sys.argv[1])
    calls = json.loads((directory / "calls.json").read_text())
    calls.append(sys.argv[2:])
    (directory / "calls.json").write_text(json.dumps(calls))
    attempt = json.loads((directory / "script.json").read_text())[len(calls) - 1]
    sys.stdout.write(attempt["output"])
    sys.exit(attempt["status"])
""")


def binary(name, outcomes, colored=False):
    """libtest output of one test binary, as cargo prints it."""
    running = f"     Running tests/{name}.rs (target/debug/deps/{name}-0123456789abcdef)"
    if colored:
        running = running.replace("Running", "\x1b[1m\x1b[92mRunning\x1b[0m")
    lines = [running, "", f"running {len(outcomes)} tests"]
    lines += [f"test {test} ... {outcome}" for test, outcome in outcomes.items()]
    failed = [test for test, outcome in outcomes.items() if outcome == "FAILED"]
    passed = len(outcomes) - len(failed)
    if failed:
        lines += ["", "failures:", ""] + [f"---- {test} stdout ----\npanicked" for test in failed]
        lines += ["", "failures:"] + [f"    {test}" for test in failed]
    verdict = "FAILED" if failed else "ok"
    lines += ["", f"test result: {verdict}. {passed} passed; {len(failed)} failed; 0 ignored; "
              f"0 measured; 0 filtered out; finished in 0.01s", ""]
    return "\n".join(lines) + "\n"


class RetryFailedTestsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="retry-failed-tests-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        (self.directory / "fake_test_command.py").write_text(FAKE_TEST_COMMAND)
        (self.directory / "calls.json").write_text("[]")

    def run_wrapper(self, attempts_output, command_args=("test", "-p", "appflowy-ai", "--",
                                                        "--test-threads=1")):
        (self.directory / "script.json").write_text(json.dumps(
            [{"output": output, "status": status} for output, status in attempts_output]
        ))
        command = [sys.executable, str(self.directory / "fake_test_command.py"),
                   str(self.directory), *command_args]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = retry.main(["--attempts", "3", "--", *command])
        calls = json.loads((self.directory / "calls.json").read_text())
        return status, stdout.getvalue(), calls

    def test_a_passing_run_runs_once(self):
        status, output, calls = self.run_wrapper([(binary("main", {"a::one": "ok"}), 0)])
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("::warning", output)
        self.assertIn("test a::one ... ok", output)

    def test_a_test_that_passes_on_a_rerun_passes_with_a_warning(self):
        first = binary("main", {"a::one": "ok", "a::two": "FAILED"}, colored=True)
        status, output, calls = self.run_wrapper([
            (first, 101), (binary("main", {"a::two": "ok"}), 0),
        ])
        self.assertEqual(status, 0)
        self.assertEqual(calls[1], ["test", "-p", "appflowy-ai", "--", "--test-threads=1",
                                    "--exact", "a::two"])
        self.assertIn("::warning title=Passed on a rerun::1 test(s) failed, then passed on a "
                      "rerun: a::two", output)
        self.assertNotIn("::error", output)

    def test_failures_in_two_binaries_are_rerun_until_each_passes(self):
        first = binary("main", {"a::one": "FAILED"}) + binary("other", {"b::two": "FAILED"})
        status, output, calls = self.run_wrapper([
            (first, 101),
            (binary("main", {"a::one": "ok"}) + binary("other", {"b::two": "FAILED"}), 101),
            (binary("main", {}) + binary("other", {"b::two": "ok"}), 0),
        ])
        self.assertEqual(status, 0)
        self.assertEqual(calls[1][-3:], ["--exact", "a::one", "b::two"])
        self.assertEqual(calls[2][-2:], ["--exact", "b::two"])
        self.assertIn("passed on a rerun: a::one, b::two", output)

    def test_a_test_that_fails_every_attempt_fails_the_run(self):
        failing = binary("main", {"a::two": "FAILED"})
        status, output, calls = self.run_wrapper([(failing, 101)] * 3)
        self.assertEqual(status, 101)
        self.assertEqual(len(calls), 3)
        self.assertIn("::error title=Failed every attempt::1 test(s) failed all 3 attempts: "
                      "a::two", output)

    def test_a_crashed_test_binary_is_not_rerun(self):
        # The second binary stopped without a result, e.g. on a stack overflow.
        crashed = binary("main", {"a::two": "FAILED"}) + (
            "     Running tests/other.rs (target/debug/deps/other-0123456789abcdef)\n\n"
            "running 2 tests\ntest b::one ... ok\n"
            "error: test failed, to rerun pass `--test other`\n"
        )
        status, output, calls = self.run_wrapper([(crashed, 101)])
        self.assertEqual(status, 101)
        self.assertEqual(len(calls), 1)
        self.assertIn("::error title=Not rerun::", output)

    def test_a_build_error_is_not_rerun(self):
        status, output, calls = self.run_wrapper([
            ("error[E0425]: cannot find value `x` in this scope\n", 101),
        ])
        self.assertEqual((status, len(calls)), (101, 1))
        self.assertIn("::error title=Not rerun::", output)

    def test_failures_libtest_did_not_name_are_not_rerun(self):
        unnamed = binary("main", {"a::one": "ok"}).replace("1 passed; 0 failed", "1 passed; 1 failed")
        status, _output, calls = self.run_wrapper([(unnamed, 101)])
        self.assertEqual((status, len(calls)), (101, 1))

    def test_a_rerun_that_does_not_report_the_test_fails_the_run(self):
        status, output, calls = self.run_wrapper([
            (binary("main", {"a::two": "FAILED"}), 101), (binary("main", {}), 0),
        ])
        self.assertEqual((status, len(calls)), (1, 2))
        self.assertIn("::error title=Rerun incomplete::", output)

    def test_a_command_without_libtest_arguments_gets_a_separator(self):
        status, _output, calls = self.run_wrapper(
            [(binary("main", {"a::two": "FAILED"}), 101), (binary("main", {"a::two": "ok"}), 0)],
            command_args=("run", "test", "ai_test::"),
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls[1], ["run", "test", "ai_test::", "--", "--exact", "a::two"])

    def test_precompiled_binaries_count_as_test_binaries(self):
        precompiled = binary("main", {"a::two": "FAILED"}).replace(
            "     Running tests/main.rs", "Running precompiled main")
        crashed = precompiled + "Running precompiled other (target/debug/deps/other-0123)\n"
        status, _output, calls = self.run_wrapper([(crashed, 134)])
        self.assertEqual((status, len(calls)), (134, 1))


if __name__ == "__main__":
    unittest.main()
