"""Exercise verified binary installation without touching host tools or starting a VM."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

import setup_macos_docker as setup


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o755
            stream.addfile(member, io.BytesIO(content))
    return output.getvalue()


class PrebuiltDockerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="prebuilt-docker-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "owned-tools"
        self.contents = {
            "lima": archive({"bin/limactl": b"lima", "share/lima/template.yaml": b"guest"}),
            "colima": b"colima-binary",
            "docker": archive({"docker/docker": b"docker-binary"}),
            "compose": b"compose-binary",
            "buildx": b"buildx-binary",
        }
        self.manifest = {
            name: {"url": f"https://example.invalid/{name}",
                   "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in self.contents.items()
        }

    def fetch(self, url, destination):
        destination.write_bytes(self.contents[url.rsplit("/", 1)[1]])

    def prepare(self):
        assets = setup.download_tools(self.root, self.manifest, self.fetch)
        return setup.install_tools(self.root, assets)

    def test_verified_archives_preserve_lima_layout_and_install_cli_plugins(self):
        prefix = self.prepare()
        self.assertEqual((prefix / "bin/docker").read_bytes(), b"docker-binary")
        self.assertEqual((prefix / "bin/colima").read_bytes(), b"colima-binary")
        self.assertEqual((prefix / "share/lima/template.yaml").read_bytes(), b"guest")
        for name in ("compose", "buildx"):
            plugin = self.root / "docker/cli-plugins" / f"docker-{name}"
            self.assertEqual(plugin.read_bytes(), self.contents[name])
            self.assertTrue(os.access(plugin, os.X_OK))

    def test_checksum_failure_even_on_last_asset_installs_nothing(self):
        self.manifest["buildx"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch for buildx"):
            setup.download_tools(self.root, self.manifest, self.fetch)
        self.assertFalse((self.root / "tools").exists())
        self.assertFalse((self.root / "docker").exists())

    def test_download_error_propagates_without_installing_partial_tools(self):
        def fail(url, destination):
            if url.endswith("/docker"):
                raise OSError("transport interrupted")
            self.fetch(url, destination)

        with self.assertRaisesRegex(OSError, "transport interrupted"):
            setup.download_tools(self.root, self.manifest, fail)
        self.assertFalse((self.root / "tools").exists())

    def test_existing_directory_is_never_overwritten(self):
        self.root.mkdir()
        marker = self.root / "existing-configuration"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            setup.download_tools(self.root, self.manifest, self.fetch)
        self.assertEqual(marker.read_text(), "keep")

    def test_failed_vm_or_daemon_does_not_publish_environment(self):
        prefix = self.prepare()
        for failure in ("colima", "docker"):
            with self.subTest(failure=failure):
                github_env = self.root / f"env-{failure}"
                github_path = self.root / f"path-{failure}"
                github_env.write_text("existing=value\n")
                github_path.write_text("existing-path\n")

                def run(command, **kwargs):
                    if command[0] == failure:
                        raise subprocess.CalledProcessError(1, command)

                with patch.dict(os.environ, {"GITHUB_ENV": str(github_env),
                                             "GITHUB_PATH": str(github_path)}), \
                     patch.object(setup.subprocess, "check_output", return_value=str(8 * 1024 ** 3)), \
                     patch.object(setup.subprocess, "run", side_effect=run):
                    with self.assertRaises(subprocess.CalledProcessError):
                        setup.start_runtime(self.root, prefix)
                self.assertEqual(github_env.read_text(), "existing=value\n")
                self.assertEqual(github_path.read_text(), "existing-path\n")

    def test_successful_vm_publishes_only_owned_config_and_verified_tools(self):
        prefix = self.prepare()
        github_env, github_path = self.root / "env", self.root / "path"
        with patch.dict(os.environ, {"GITHUB_ENV": str(github_env),
                                     "GITHUB_PATH": str(github_path)}), \
             patch.object(setup.subprocess, "check_output", return_value=str(8 * 1024 ** 3)), \
             patch.object(setup.subprocess, "run") as run:
            setup.start_runtime(self.root, prefix)
        self.assertEqual(github_path.read_text(), f"{prefix / 'bin'}\n")
        self.assertEqual(github_env.read_text(),
                         f"DOCKER_CONFIG={self.root / 'docker'}\n"
                         f"COLIMA_HOME={self.root / 'colima'}\nLIMA_HOME={self.root / 'lima'}\n")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:2], ["colima", "start"])
        self.assertIn("--arch", commands[0])
        self.assertEqual(commands[1:], [["docker", "info"], ["docker", "compose", "version"],
                                       ["docker", "buildx", "version"], ["colima", "version"]])
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))
        self.assertTrue(all(call.kwargs["env"]["DOCKER_CONFIG"] == str(self.root / "docker")
                            for call in run.call_args_list))

    def test_setup_refuses_local_or_arm_machines_before_installation(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "RUNNER_OS": "macOS",
                                     "RUNNER_ARCH": "X64"}), \
             patch.object(setup.platform, "system", return_value="Darwin"), \
             patch.object(setup.platform, "machine", return_value="x86_64"):
            with self.assertRaises(ValueError):
                setup.require_intel_runner()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_OS": "macOS",
                                     "RUNNER_ARCH": "ARM64"}), \
             patch.object(setup.platform, "system", return_value="Darwin"), \
             patch.object(setup.platform, "machine", return_value="arm64"):
            with self.assertRaises(ValueError):
                setup.require_intel_runner()
        self.assertFalse(self.root.exists())

    def test_workflow_keeps_real_tests_and_uses_pinned_prebuilt_setup(self):
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((root / ".github/workflows/ios_ci.yaml").read_text())
        job = workflow["jobs"]["integration-tests"]
        self.assertEqual(job["timeout-minutes"], 120)
        step = next(s for s in job["steps"] if s.get("id") == "cloud-docker")
        self.assertEqual(step["run"], "python3 .mobile-ci-tools/.github/scripts/setup_macos_docker.py")
        self.assertNotIn("uses", step)
        self.assertEqual(step["timeout-minutes"], 10)
        cloud_tests = next(s for s in job["steps"] if s.get("id") == "cloud-tests")
        self.assertIn("mobile_row_page_cloud_runner.dart", cloud_tests["run"])
        manifest = json.loads(setup.MANIFEST.read_text())
        self.assertEqual(set(manifest), {"lima", "colima", "docker", "compose", "buildx"})
        for asset in manifest.values():
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(asset["url"].startswith(("https://github.com/", "https://download.docker.com/")))
            self.assertNotIn("/latest/", asset["url"])


if __name__ == "__main__":
    unittest.main()
