"""Run source toolchain selection from the actual Desktop CI workflow steps."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
WORKFLOW_NAMES = ("rust_ci.yaml", "flutter_ci.yaml", "ios_ci.yaml")
SELECT_STEP = "Select Rust toolchain from the tested Desktop revision"


class DesktopToolchainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = {
            name: yaml.safe_load((WORKFLOWS / name).read_text())
            for name in WORKFLOW_NAMES
        }
        cls.selections = []
        for name, workflow in cls.workflows.items():
            for job, config in workflow["jobs"].items():
                for step in config.get("steps", []):
                    if step.get("name") == SELECT_STEP:
                        cls.selections.append((name, job, step))

    def run_selection(self, step, contents):
        with tempfile.TemporaryDirectory(prefix="desktop-source-toolchain-") as directory:
            root = Path(directory)
            (root / "frontend").mkdir()
            # The public CI checkout or ambient version must not override the
            # private Desktop source selected for this particular run.
            (root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.92.0"\n')
            if contents is not None:
                (root / "frontend/rust-toolchain.toml").write_text(contents)
            output = root / "github-env"
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                cwd=root,
                env={**os.environ, "GITHUB_ENV": str(output), "RUST_TOOLCHAIN": "1.92.0"},
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result, output.read_text() if output.exists() else ""

    def test_every_install_uses_its_checked_out_source_and_keeps_target_components(self):
        installs = 0
        for name, workflow in self.workflows.items():
            self.assertNotIn("RUST_TOOLCHAIN", workflow.get("env", {}))
            for job, config in workflow["jobs"].items():
                steps = config.get("steps", [])
                for index, install in enumerate(steps):
                    if install.get("name") != "Install Rust toolchain":
                        continue
                    with self.subTest(workflow=name, job=job):
                        installs += 1
                        self.assertEqual(steps[index - 1]["name"], SELECT_STEP)
                        python = steps[index - 2]
                        self.assertEqual(python["uses"], "actions/setup-python@v5")
                        self.assertEqual(python["with"]["python-version"], "3.12")
                        self.assertTrue(any(
                            step.get("uses", "").startswith("actions/checkout@")
                            and step.get("with", {}).get("repository") == "AppFlowy-IO/AppFlowy-Premium"
                            for step in steps[:index - 2]
                        ))
                        self.assertEqual(install["uses"], "dtolnay/rust-toolchain@stable")
                        self.assertEqual(install["with"]["toolchain"], "${{ env.RUST_TOOLCHAIN }}")
                        self.assertNotIn("profile", install["with"])
                        self.assertNotIn("override", install["with"])
                        if name == "rust_ci.yaml":
                            self.assertEqual(install["with"]["components"], "rustfmt, clippy")
                        elif name == "ios_ci.yaml":
                            self.assertEqual(install["with"]["target"], "aarch64-apple-ios-sim")
                        else:
                            self.assertEqual(install["with"]["target"], "${{ matrix.target }}")
        self.assertEqual(installs, 8)
        self.assertEqual(len(self.selections), installs)

    def test_old_and_new_source_pins_override_ambient_ci_version(self):
        for name, job, step in self.selections:
            for channel in ("1.92.0", "1.98.0", "1.100.1"):
                with self.subTest(workflow=name, job=job, channel=channel):
                    result, output = self.run_selection(
                        step,
                        f"# Source-owned Rust version\n[toolchain]\nchannel = '{channel}' # pinned\n"
                        'components = ["clippy", "rustfmt"]\n',
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output, f"RUST_TOOLCHAIN={channel}\n")

    def test_missing_malformed_or_floating_source_never_falls_back(self):
        cases = [
            None,
            "not valid TOML",
            '[other]\nchannel = "1.98.0"\n',
            '[toolchain]\ncomponents = ["rustfmt"]\n',
            '[toolchain]\nchannel = "stable"\n',
            '[toolchain]\nchannel = "nightly-2026-10-13"\n',
            '[toolchain]\nchannel = "1.98"\n',
            '[toolchain]\nchannel = "1.98.0"\nchannel = "1.92.0"\n',
            '[toolchain]\nchannel = "1.98.0\\nRUSTUP_TOOLCHAIN=nightly"\n',
            '[toolchain]\nchannel = "$(touch injected)"\n',
        ]
        for name, job, step in self.selections:
            for contents in cases:
                with self.subTest(workflow=name, job=job, contents=contents):
                    result, output = self.run_selection(step, contents)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
