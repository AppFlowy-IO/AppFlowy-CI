"""Exercise the active Cloud workflow's historical fixture and client test commands."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
CLIENT_URL = "https://github.com/AppFlowy-IO/AppFlowy-Client.git"
AUTHORIZATION = "AUTHORIZATION: basic synthetic-test-credential"


class RowDocumentHistoryWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.steps = cls.workflow["jobs"]["test"]["steps"]
        cls.fixture = next(
            step for step in cls.steps
            if step.get("name") == "Build historical row document fixture generator"
        )
        cls.tests = next(step for step in cls.steps if step.get("name") == "Run Tests")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.calls = self.directory / "calls.jsonl"
        self.env = {
            "PATH": str(self.directory / "bin") + os.pathsep + os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "CARGO_CALLS": str(self.calls),
            "TEST_SKIPS": "",
        }
        self.git("init", "--quiet")
        self.git("init", "--bare", "--quiet", "cargo-cache")
        self.git("config", "--local", "http.https://github.com/.extraheader", AUTHORIZATION)
        binary = self.directory / "bin/cargo"
        binary.parent.mkdir()
        binary.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
def header(url):
    result = subprocess.run(
        ["git", "-C", "cargo-cache", "config", "--get-urlmatch", "http.extraheader", url],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip()
with Path(os.environ["CARGO_CALLS"]).open("a") as output:
    output.write(json.dumps({
        "args": sys.argv[1:],
        "git_cli": os.environ.get("CARGO_NET_GIT_FETCH_WITH_CLI"),
        "client_header": header("https://github.com/AppFlowy-IO/AppFlowy-Client.git/info/refs"),
        "other_header": header("https://github.com/other/repository.git/info/refs"),
    }) + "\\n")
exit_code = os.environ.get("CARGO_EXIT", "0")
if "client-api" in sys.argv:
    exit_code = os.environ.get("CLIENT_TEST_EXIT", exit_code)
sys.exit(int(exit_code))
''')
        binary.chmod(0o755)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.directory, env=self.env,
            capture_output=True, text=True, check=True,
        )

    def run_step(self, step, **env):
        script = step["run"].replace(
            "${{ matrix.test_service }}", "appflowy_cloud_root_unit",
        ).replace("${{ matrix.test_modules }}", "")
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            cwd=self.directory, env={**self.env, **env},
            capture_output=True, text=True, check=False, timeout=10,
        )

    def cargo_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_fixture_build_scopes_auth_without_persisting_or_printing_it(self):
        before = (self.directory / ".git/config").read_bytes()
        result = self.run_step(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        [call] = self.cargo_calls()
        self.assertEqual(call["args"], [
            "build", "--locked", "--manifest-path",
            "tests/tools/legacy_row_history/Cargo.toml", "--target-dir",
            str(self.directory.resolve() / "target/row-document-history-generator"),
        ])
        self.assertEqual(call["git_cli"], "true")
        self.assertEqual(call["client_header"], AUTHORIZATION)
        self.assertEqual(call["other_header"], "")
        self.assertNotIn(AUTHORIZATION, result.stdout + result.stderr)
        self.assertEqual((self.directory / ".git/config").read_bytes(), before)
        self.assertNotIn(AUTHORIZATION, (self.directory / "cargo-cache/config").read_text())

    def test_missing_checkout_credential_fails_before_build(self):
        self.git("config", "--local", "--unset", "http.https://github.com/.extraheader")
        result = self.run_step(self.fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkout credential is required", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_fixture_build_failure_is_not_hidden(self):
        result = self.run_step(self.fixture, CARGO_EXIT="13")
        self.assertEqual(result.returncode, 13)

    def test_only_collab_lane_prebuilds_available_generator_before_tests(self):
        self.assertEqual(self.fixture["if"], (
            "matrix.test_service == 'appflowy_cloud_collab' && "
            "hashFiles('tests/tools/legacy_row_history/Cargo.toml') != ''"
        ))
        self.assertLess(self.steps.index(self.fixture), self.steps.index(self.tests))
        self.assertNotIn("continue-on-error", self.fixture)

    def test_root_lane_runs_client_library_tests_after_server_units(self):
        result = self.run_step(self.tests)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.cargo_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("--bins", calls[0]["args"])
        self.assertEqual(calls[1]["args"], [
            "test", "--locked", "-p", "client-api", "--lib", "--", "--test-threads=1",
        ])
        self.assertEqual(calls[1]["client_header"], "")

    def test_client_library_failure_fails_the_root_unit_lane(self):
        result = self.run_step(self.tests, CLIENT_TEST_EXIT="17")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(len(self.cargo_calls()), 2)


if __name__ == "__main__":
    unittest.main()
