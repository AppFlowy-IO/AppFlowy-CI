"""Exercise Git's URL matching and the real child-process authentication boundary."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from with_private_cargo_auth import command_environment


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / ".github/scripts/with_private_cargo_auth.py"


class PrivateCargoAuthTest(unittest.TestCase):
    def test_git_scopes_the_header_and_keeps_inherited_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "gitconfig"
            config.write_text("[user]\nname = unchanged\n")
            before = config.read_bytes()
            inherited = {
                **os.environ, "PRESENTATION_CARGO_TOKEN": "test-token",
                "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "test.preserved",
                "GIT_CONFIG_VALUE_0": "yes",
            }
            environment, credential = command_environment(inherited)
            self.assertEqual(inherited["GIT_CONFIG_COUNT"], "1")
            self.assertNotIn("PRESENTATION_CARGO_TOKEN", environment)
            self.assertEqual(environment["CARGO_NET_GIT_FETCH_WITH_CLI"], "true")
            for url, allowed in [
                ("https://github.com/AppFlowy-IO/AppFlowy-Cloud-Premium.git", True),
                ("https://github.com/AppFlowy-IO/AppFlowy-Client", True),
                ("https://github.com/another-owner/repo", False),
                ("https://github.com/AppFlowy-IO-other/repo", False),
                ("https://github.com.evil.example/AppFlowy-IO/repo", False),
                ("http://github.com/AppFlowy-IO/repo", False),
            ]:
                with self.subTest(url=url):
                    result = subprocess.run(
                        ["git", "config", "--get-urlmatch", "http.extraheader", url],
                        cwd=directory, env=environment, text=True, capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0 if allowed else 1)
                    self.assertEqual(result.stdout.strip(),
                                     f"AUTHORIZATION: basic {credential}" if allowed else "")
            result = subprocess.check_output(
                ["git", "config", "test.preserved"], cwd=directory, env=environment, text=True)
            self.assertEqual(result.strip(), "yes")
            self.assertEqual(config.read_bytes(), before)

    def test_missing_secret_fails_before_the_child_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started"
            environment = {**os.environ, "PRESENTATION_CARGO_TOKEN": ""}
            result = subprocess.run(
                [sys.executable, str(WRAPPER), sys.executable, "-c",
                 f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                env=environment, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(marker.exists())

    def test_child_failure_is_preserved_without_logging_the_secret(self):
        environment = {**os.environ, "PRESENTATION_CARGO_TOKEN": "test-token",
                       "GITHUB_ACTIONS": "false"}
        result = subprocess.run(
            [sys.executable, str(WRAPPER), sys.executable, "-c", "raise SystemExit(17)"],
            env=environment, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 17)
        self.assertEqual(result.stdout + result.stderr, "")

    def test_native_workflow_wraps_the_runner_with_the_private_checkout_token(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/cloud_integration_ci.yaml").read_text())
        steps = workflow["jobs"]["presentation-acceptance"]["steps"]
        step = next(s for s in steps if s.get("name") ==
                    "Run native source ACL, private S3 and actual Gateway contracts")
        self.assertEqual(step["env"]["PRESENTATION_CARGO_TOKEN"], "${{ secrets.ADMIN_GITHUB_TOKEN }}")
        self.assertIn("with_private_cargo_auth.py python3 -B", step["run"])
        self.assertIn("presentation_acceptance_runner.py", step["run"])
        self.assertTrue(all(s["with"]["persist-credentials"] is False for s in steps
                            if s.get("uses") == "actions/checkout@v4"))


if __name__ == "__main__":
    unittest.main()
