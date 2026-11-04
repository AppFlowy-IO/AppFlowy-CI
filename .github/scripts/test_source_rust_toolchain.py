"""Keep source compiler selection consistent with rustup's checkout override."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from source_rust_toolchain import source_toolchain


class SourceToolchainTests(unittest.TestCase):
    def test_source_pin_overrides_an_older_ci_default(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.98.1"\n')
            self.assertEqual(source_toolchain(directory, "1.98.0"), "1.98.1")

    def test_legacy_sources_keep_the_caller_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(source_toolchain(directory, "1.98.0"), "1.98.0")

    def test_workflow_resolves_and_forwards_the_immutable_checkout_pin(self):
        repository = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((repository / ".github/workflows/cloud_integration_ci.yaml").read_text())
        source_job = workflow["jobs"]["image_source"]
        step = next(item for item in source_job["steps"] if item.get("id") == "toolchain")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.98.1"\n')
            tools = source / "ci-tools/.github/scripts"
            tools.mkdir(parents=True)
            shutil.copy(repository / ".github/scripts/source_rust_toolchain.py", tools)
            output = source / "output"
            subprocess.run(["bash", "-eu", "-c", step["run"]], cwd=source, check=True,
                           env=os.environ | {"RUST_TOOLCHAIN": "1.98.0", "GITHUB_OUTPUT": str(output)},
                           capture_output=True, text=True)
            self.assertEqual(output.read_text(), "rust_toolchain=1.98.1\n")
        self.assertEqual(source_job["outputs"]["rust_toolchain"],
                         "${{ steps.toolchain.outputs.rust_toolchain }}")
        request = next(item for item in workflow["jobs"]["build_self_hosted"]["steps"]
                       if item.get("name") == "Request one private build and wait for its artifacts")
        self.assertEqual(request["env"]["RUST_TOOLCHAIN"],
                         "${{ needs.image_source.outputs.rust_toolchain }}")

    def test_unpinned_source_does_not_silently_use_the_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "rust-toolchain.toml")
            for channel in ('"stable"', '"1.98.1\\nother=value"', '[]'):
                path.write_text(f"[toolchain]\nchannel = {channel}\n")
                with self.subTest(channel=channel), self.assertRaises(ValueError):
                    source_toolchain(directory, "1.98.0")


if __name__ == "__main__":
    unittest.main()
