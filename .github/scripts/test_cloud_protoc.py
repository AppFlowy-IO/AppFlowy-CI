from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml
import cloud_protoc


class CloudProtocTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for owner in ("appflowy-proto", "collab"):
            crate = self.root / "libs" / owner
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").write_text('[build-dependencies]\nprotoc-bin-vendored = "3"\n')
            (crate / "build.rs").write_text("fn main() { protoc_bin_vendored::protoc_bin_path().unwrap(); }")

    def test_current_source_compiles_without_system_installation(self):
        with patch.object(cloud_protoc.subprocess, "run") as run:
            cloud_protoc.prepare(self.root)
        run.assert_not_called()

    def test_legacy_owner_without_its_own_compiler_reuses_system_protoc(self):
        (self.root / "libs/collab/build.rs").write_text("fn main() {}")
        with patch.object(cloud_protoc.shutil, "which", return_value="/bin/protoc"), \
                patch.object(cloud_protoc.subprocess, "run", return_value=Mock(returncode=0)) as run:
            cloud_protoc.prepare(self.root)
        run.assert_called_once_with(["/bin/protoc", "--version"])

    def test_older_layout_installs_and_checks_the_missing_compiler(self):
        (self.root / "libs/appflowy-proto/Cargo.toml").unlink()
        with patch.object(cloud_protoc.shutil, "which", return_value=None), \
                patch.object(cloud_protoc.subprocess, "run") as run:
            cloud_protoc.prepare(self.root)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-1], "update")
        self.assertIn("install", commands[1])
        self.assertIn("protobuf-compiler", commands[1])
        self.assertEqual(commands[2], ["protoc", "--version"])
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))

    def test_failed_package_installation_on_legacy_source_stops_the_build(self):
        (self.root / "libs/collab/Cargo.toml").write_text("[build-dependencies]\n")
        failed = subprocess.CalledProcessError(100, ["apt-get", "install"])
        with patch.object(cloud_protoc.shutil, "which", return_value="/bin/protoc"), \
                patch.object(cloud_protoc.subprocess, "run",
                             side_effect=[Mock(returncode=1), Mock(returncode=0), failed]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                cloud_protoc.prepare(self.root)
        self.assertEqual(run.call_count, 3)

    def test_compilation_jobs_use_the_trusted_ci_helper(self):
        workflow = Path(__file__).resolve().parents[1] / "workflows/cloud_integration_ci.yaml"
        jobs = yaml.safe_load(workflow.read_text())["jobs"]
        steps = [step for job in jobs.values() for step in job.get("steps", [])
                 if step.get("name") == "Select the Cloud protobuf compiler"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["if"], "env.CLOUD_COMPILE_TESTS == 'true'")
        self.assertEqual(steps[0]["run"], "python3 -B ci-tools/.github/scripts/cloud_protoc.py")

        matrix_steps = jobs["test"]["steps"]
        checkout = next(step for step in matrix_steps if step.get("name") == "Checkout CI tools")
        self.assertNotIn("if", checkout, "package-only compilers also need the CI helper")
        self.assertLess(matrix_steps.index(checkout), matrix_steps.index(steps[0]))
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["with"]["path"], "ci-tools")
