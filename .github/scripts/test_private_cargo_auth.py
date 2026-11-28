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

    def test_legacy_smoke_prefetches_git_objects_without_executing_its_source_script(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/cloud_integration_ci.yaml").read_text())
        step = next(s for s in workflow["jobs"]["test"]["steps"] if s.get("name", "").startswith("Fetch released"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote"
            subprocess.run(["git", "init", "-q", "-b", "main", str(remote)], check=True)
            commits = []
            for message in ("first release", "unselected commit", "second release", "current"):
                subprocess.run(["git", "-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                                "-c", "commit.gpgsign=false", "commit", "-qm", message, "--allow-empty"],
                               cwd=remote, check=True)
                commits.append(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=remote, text=True).strip())
            for folder, pins in (("listed", commits[::2]), ("old-format", [])):
                with self.subTest(folder=folder):
                    checkout = root / folder
                    subprocess.run(["git", "clone", "--quiet", "--depth=1", remote.as_uri(), str(checkout)], check=True)
                    script = checkout / "tests/legacy_client_smoke/checkout_legacy.sh"
                    script.parent.mkdir(parents=True)
                    script.write_text("touch should-never-execute\n" + "".join(
                        f"  0.1.{n}) commit={sha} ;;\n" for n, sha in enumerate(pins)))
                    subprocess.run(["git", "config", f"url.{remote.as_uri()}.insteadOf",
                                    "https://github.com/AppFlowy-IO/AppFlowy-Cloud-Premium.git"], cwd=checkout, check=True)
                    subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", step["run"]], cwd=checkout, check=True)
                    for sha in commits[::2]:
                        subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"], cwd=checkout, check=True)
                    self.assertFalse((checkout / "should-never-execute").exists())
                    self.assertNotIn("credential", (checkout / ".git/config").read_text())
                    shallow = subprocess.check_output(["git", "rev-parse", "--is-shallow-repository"],
                                                       cwd=checkout, text=True).strip()
                    self.assertEqual(shallow, "true" if pins else "false")

    def test_source_tests_run_after_authenticated_dependency_fetches(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/cloud_integration_ci.yaml").read_text())
        jobs = workflow["jobs"]
        steps = jobs["presentation-acceptance"]["steps"]
        run = next(s for s in steps if s.get("name") == "Run native source ACL, private S3 and actual Gateway contracts")
        fetch = next(s for s in steps if "PRESENTATION_CARGO_TOKEN" in s.get("env", {}))
        self.assertLess(steps.index(fetch), steps.index(run))
        self.assertIn("presentation_images != 'true'", fetch["if"])
        self.assertIn("cargo fetch --locked", fetch["run"])
        self.assertEqual(fetch["working-directory"], "${{ runner.temp }}")
        self.assertNotIn("PRESENTATION_CARGO_TOKEN", run["env"])
        self.assertNotIn("with_private_cargo_auth", run["run"])
        self.assertIn("presentation_acceptance_runner.py", run["run"])
        for job in jobs.values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@") and "token" in step.get("with", {}):
                    self.assertIs(step["with"].get("persist-credentials"), False)
        legacy = next(s for s in jobs["test"]["steps"] if s.get("name", "").startswith("Fetch released Cloud revisions"))
        self.assertIn("appflowy_cloud_legacy_client_smoke", legacy["if"])
        self.assertIn("gh auth git-credential", legacy["run"])



if __name__ == "__main__":
    unittest.main()
