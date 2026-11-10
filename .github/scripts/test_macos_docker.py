"""Exercise the job-owned macOS installer without installing host packages or VMs."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

import macos_docker as docker


ROOT = Path(__file__).resolve().parents[2]


class MacosDockerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.area = Path(self.directory.name)
        self.root = self.area / "job"

    def asset(self, name, payload, destination, *, archive=False, member=None):
        path = self.area / name
        if archive:
            with tarfile.open(path, "w:gz") as bundle:
                for filename, content in payload.items():
                    info = tarfile.TarInfo(filename)
                    info.size = len(content)
                    info.mode = 0o755
                    bundle.addfile(info, io.BytesIO(content))
        else:
            path.write_bytes(payload)
        asset = {"name": name, "version": "1.2.3", "url": "https://example.invalid/" + name,
                 "sha256": docker.checksum(path), "format": "tar" if archive else "file",
                 "destination": destination}
        if member:
            asset["member"] = member
        return asset

    def fetch(self, asset, directory):
        return self.area / asset["name"]

    def test_installs_lima_tree_docker_archive_and_plugins_in_owned_prefix(self):
        assets = [self.asset("lima", {"bin/limactl": b"lima", "libexec/lima/guestagent": b"agent"},
                             "tools", archive=True),
                  self.asset("docker", {"docker/docker": b"docker"}, "tools/bin/docker",
                             archive=True, member="docker/docker"),
                  self.asset("compose", b"compose", "docker/cli-plugins/docker-compose")]
        docker.install_tools(self.root, assets, self.fetch)
        self.assertEqual((self.root / "tools/bin/docker").read_bytes(), b"docker")
        self.assertEqual((self.root / "tools/libexec/lima/guestagent").read_bytes(), b"agent")
        plugin = self.root / "docker/cli-plugins/docker-compose"
        self.assertEqual(plugin.read_bytes(), b"compose")
        self.assertEqual(plugin.stat().st_mode & 0o777, 0o755)

    def test_checksum_mismatch_prevents_installation(self):
        asset = self.asset("colima", b"valid", "tools/bin/colima")
        (self.area / "colima").write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
            docker.install_tools(self.root, [asset], self.fetch)
        self.assertFalse((self.root / "tools/bin/colima").exists())

    def test_rejects_archive_and_destination_paths_outside_owned_prefix(self):
        asset = self.asset("lima", {"../../escaped": b"untrusted"}, "tools", archive=True)
        with self.assertRaises(tarfile.FilterError):
            docker.install_tools(self.root, [asset], self.fetch)
        self.assertFalse((self.area / "escaped").exists())
        asset = self.asset("colima", b"binary", "../escaped")
        with self.assertRaises(ValueError):
            docker.install_tools(self.root, [asset], self.fetch)
        self.assertFalse((self.area / "escaped").exists())

    def test_download_is_bounded_verified_and_reuses_only_valid_cache(self):
        asset = self.asset("docker", b"verified bytes", "tools/bin/docker")
        def curl(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"verified bytes")
            self.assertEqual(kwargs["timeout"], 330)
            self.assertIn("--max-time", command)
            self.assertNotIn("--insecure", command)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(docker.subprocess, "run", side_effect=curl) as run:
            path = docker.download(asset, self.root / "downloads")
            docker.download(asset, self.root / "downloads")
            self.assertEqual(run.call_count, 1)
            path.write_bytes(b"corrupt cache")
            docker.download(asset, self.root / "downloads")
            self.assertEqual(run.call_count, 2)

    def test_wrong_download_bytes_are_removed(self):
        asset = self.asset("docker", b"valid", "tools/bin/docker")
        def curl(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"wrong")
        with patch.object(docker.subprocess, "run", side_effect=curl):
            with self.assertRaises(RuntimeError):
                docker.download(asset, self.root / "downloads")
        self.assertFalse((self.root / "downloads/docker").exists())

    def test_new_lima_deployment_target_fails_on_macos_15(self):
        # Observed in the official 2.3.0, 2.2.1 and 2.2.0 Intel artifacts.
        metadata = "cmd LC_BUILD_VERSION\nplatform MACOS\nminos 26.0\nsdk 26.5\nntools 1\ntool LD\nversion 1267.0\n"
        with patch.object(docker.subprocess, "check_output", return_value=metadata):
            with self.assertRaisesRegex(RuntimeError, "requires macOS 26.0; runner is macOS 15.7"):
                docker.verify_macos_version(Path("limactl"), "15.7")

    def test_compatible_modern_and_legacy_deployment_targets(self):
        for metadata in ("minos 15.0.0\nversion 1167.5\n", "cmd LC_VERSION_MIN_MACOSX\nversion 12.0\nsdk 12.0\n"):
            with self.subTest(metadata=metadata):
                with patch.object(docker.subprocess, "check_output", return_value=metadata):
                    docker.verify_macos_version(Path("docker"), "15.0")
        with patch.object(docker.subprocess, "check_output", return_value="not Mach-O metadata"):
            with self.assertRaises(RuntimeError):
                docker.verify_macos_version(Path("docker"), "15.7")

    def test_only_intel_macos_can_start_the_runtime(self):
        for system, machine in [("Linux", "x86_64"), ("Darwin", "arm64")]:
            with patch.object(docker.platform, "system", return_value=system), patch.object(
                docker.platform, "machine", return_value=machine,
            ):
                with self.assertRaises(RuntimeError):
                    docker.require_intel_macos()

    def test_runtime_reserves_host_capacity_and_requires_a_working_daemon(self):
        with patch.dict(os.environ, {"PATH": "/bin", "HOME": "/original"}, clear=True), patch.object(
            docker.subprocess, "check_output", side_effect=["4\n", str(16 * 1024 ** 3)],
        ), patch.object(docker.subprocess, "run") as run, \
                patch.object(docker, "configure_image_store") as configure:
            docker.start_runtime(self.root)
        start, = run.call_args_list
        command = start.args[0]
        self.assertEqual(command[command.index("--cpu") + 1], "2")
        self.assertEqual(command[command.index("--memory") + 1], "8")
        self.assertIn("--vm-type=vz", command)
        self.assertIn("--mount-type=virtiofs", command)
        self.assertEqual(start.kwargs["env"]["HOME"], "/original")
        self.assertEqual(start.kwargs["env"]["COLIMA_HOME"], str(self.root / "colima"))
        self.assertEqual(start.kwargs["env"]["LIMA_HOME"], str(self.root / "colima/_lima"))
        self.assertTrue((self.root / "colima").is_dir())
        configure.assert_called_once_with(self.root)

    def test_fresh_runtime_uses_owned_profile_instead_of_inherited_lima_home(self):
        tools = self.root / "tools/bin"
        tools.mkdir(parents=True)
        calls = self.area / "calls.jsonl"
        sysctl = tools / "sysctl"
        sysctl.write_text(f"#!{sys.executable}\nimport sys\n"
                         "print(4 if sys.argv[-1] == 'hw.ncpu' else 14 * 1024 ** 3)\n")
        sysctl.chmod(0o700)
        config = self.area / "daemon.json"
        original_config = {"features": {"buildkit": True, "containerd-snapshotter": True},
                           "exec-opts": ["native.cgroupdriver=cgroupfs"], "mtu": 1500}
        config.write_text(json.dumps(original_config))
        for name in ("colima", "docker", "limactl"):
            executable = tools / name
            executable.write_text(
                f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                "assert Path(os.environ['COLIMA_HOME']).is_dir()\n"
                "with Path(os.environ['CALLS']).open('a') as output:\n"
                " output.write(json.dumps({'argv': sys.argv, 'home': os.environ['HOME'], "
                "'lima': os.environ['LIMA_HOME'], 'context': os.environ.get('DOCKER_CONTEXT'), "
                "'host': os.environ.get('DOCKER_HOST')}) + '\\n')\n"
                "if sys.argv[0].endswith('/limactl'):\n"
                " config = Path(os.environ['DAEMON_CONFIG'])\n"
                " if 'cat' in sys.argv: print(config.read_text())\n"
                " elif 'tee' in sys.argv: config.write_text(sys.stdin.read())\n"
                "if sys.argv[0].endswith('/docker'): print('overlay2')\n"
            )
            executable.chmod(0o700)
        original_home = self.area / "original-home"
        original_home.mkdir()
        sentinel = original_home / "existing-profile"
        sentinel.write_text("preserve")
        with patch.dict(os.environ, {
            "PATH": str(tools), "HOME": str(original_home),
            "LIMA_HOME": str(original_home / "existing-profile"), "CALLS": str(calls),
            "DAEMON_CONFIG": str(config), "DOCKER_CONTEXT": "unrelated",
            "DOCKER_HOST": "tcp://unrelated.invalid:2375",
        }):
            docker.start_runtime(self.root)
        start, read, write, restart, ready = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(start["home"], str(original_home))
        self.assertEqual(start["lima"], str(self.root / "colima/_lima"))
        self.assertEqual(start["argv"][2:6], ["--cpu", "2", "--memory", "8"])
        self.assertEqual(read["argv"][-2:], ["cat", "/etc/docker/daemon.json"])
        self.assertEqual(write["argv"][-2:], ["tee", "/etc/docker/daemon.json"])
        self.assertEqual(restart["argv"][-3:], ["systemctl", "restart", "docker"])
        socket = f"unix://{self.root.resolve()}/colima/default/docker.sock"
        self.assertEqual(ready["argv"], [str((tools / "docker").resolve()), "--host", socket,
                                         "info", "--format", "{{.Driver}}"])
        for call in (read, write, restart, ready):
            self.assertEqual(call["host"], socket)
            self.assertIsNone(call["context"])
        expected_config = {**original_config, "features": {"buildkit": True,
                                                          "containerd-snapshotter": False}}
        self.assertEqual(json.loads(config.read_text()), expected_config)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_image_store_rejects_invalid_config_without_writing_or_restarting(self):
        for config in ("broken-json", "[]", '{"features": []}'):
            with self.subTest(config=config), patch.object(
                docker.subprocess, "check_output", return_value=config,
            ), patch.object(docker.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    docker.configure_image_store(self.root)
                run.assert_not_called()

    def test_image_store_requires_bounded_successful_restart_and_verified_driver(self):
        with patch.object(docker.subprocess, "check_output", side_effect=["{}", "overlayfs\n"]), \
                patch.object(docker.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "must use overlay2"):
                docker.configure_image_store(self.root)
        write, restart = run.call_args_list
        self.assertEqual(json.loads(write.kwargs["input"]),
                         {"features": {"containerd-snapshotter": False}})
        self.assertTrue(write.kwargs["check"])
        self.assertEqual(write.kwargs["timeout"], 30)
        self.assertTrue(restart.kwargs["check"])
        self.assertEqual(restart.kwargs["timeout"], 60)
        failure = subprocess.TimeoutExpired(["systemctl", "restart", "docker"], 60)
        with patch.object(docker.subprocess, "check_output", return_value="{}") as read, \
                patch.object(docker.subprocess, "run", side_effect=[None, failure]):
            with self.assertRaises(subprocess.TimeoutExpired):
                docker.configure_image_store(self.root)
        self.assertEqual(read.call_count, 1)

    def test_publishes_only_job_owned_tool_paths_and_configuration(self):
        path_file, env_file = self.area / "path", self.area / "env"
        with patch.dict(os.environ, {"GITHUB_PATH": str(path_file), "GITHUB_ENV": str(env_file)}):
            docker.publish_environment(self.root)
        self.assertEqual(path_file.read_text(), str(self.root / "tools/bin") + "\n")
        self.assertEqual(env_file.read_text().splitlines(), [
            f"DOCKER_CONFIG={self.root / 'docker'}", f"COLIMA_HOME={self.root / 'colima'}",
            f"LIMA_HOME={self.root / 'colima/_lima'}",
        ])

    def test_failed_vm_start_keeps_original_failure_and_bounded_owned_boot_logs(self):
        logs = self.root / "colima/_lima/colima"
        logs.mkdir(parents=True)
        (logs / "serial.log").write_text("x" * (70 * 1024) + "guest boot failure\n")
        external = self.area / "unrelated"
        external.write_text("unrelated configuration")
        (logs / "ha.stderr.log").symlink_to(external)
        failure = subprocess.CalledProcessError(7, ["colima", "start"])
        output = io.StringIO()
        with patch.object(docker.subprocess, "check_output", side_effect=["4", str(14 * 1024 ** 3)]), patch.object(
            docker.subprocess, "run", side_effect=failure,
        ) as run, contextlib.redirect_stderr(output):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                docker.start_runtime(self.root)
        self.assertIs(caught.exception, failure)
        self.assertEqual(run.call_count, 1)
        self.assertIn("guest boot failure", output.getvalue())
        self.assertNotIn("unrelated configuration", output.getvalue())
        self.assertLess(len(output.getvalue()), 66 * 1024)

    def test_runtime_failure_evidence_is_bounded_and_uses_only_the_owned_daemon(self):
        output = self.area / "evidence"
        logs = self.root / "colima/_lima/colima"
        logs.mkdir(parents=True)
        (logs / "serial.log").write_text("discarded-prefix" + "x" * 70000 + "kernel-end")
        outside = self.area / "unrelated.log"
        outside.write_text("unrelated user log")
        (logs / "serial0.log").symlink_to(outside)
        network = self.root / "colima/_lima/_networks/user-v2"
        network.mkdir(parents=True)
        (network / "usernet.stderr.log").write_text("network evidence")
        calls = []

        def run(command, **options):
            self.assertEqual(options["timeout"], 10)
            self.assertFalse(options["check"])
            self.assertNotIn("DOCKER_CONTEXT", options["env"])
            self.assertEqual(options["env"]["DOCKER_HOST"],
                             f"unix://{self.root.resolve()}/colima/default/docker.sock")
            if command[0].endswith("/docker"):
                self.assertEqual(command[1:3], ["--host", options["env"]["DOCKER_HOST"]])
                self.assertNotIn("pull", command)
                self.assertNotIn("prune", command)
            options["stdout"].write(b"discarded-command-prefix" + b"x" * 70000 + b"command-end")
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.area),
                                    "DOCKER_HOST": "tcp://unrelated.invalid:2375",
                                    "DOCKER_CONTEXT": "unrelated"}), \
                patch.object(docker.subprocess, "run", side_effect=run):
            docker.collect_runtime_diagnostics(self.root, output)
            self.assertEqual(os.environ["DOCKER_CONTEXT"], "unrelated")
        self.assertEqual(len(calls), 8)
        self.assertIn([str((self.root / "tools/bin/limactl").resolve()), "shell", "colima", "df", "-h",
                       "/", "/var/lib/docker", "/var/lib/containerd"], calls)
        journal = next(command for command in calls if "journalctl" in command)
        self.assertEqual(journal[-7:], ["--no-pager", "-n", "300", "-u", "docker", "-u", "containerd"])
        self.assertEqual((output / "docker-info.log").stat().st_size, 64 * 1024)
        self.assertTrue((output / "docker-info.log").read_bytes().endswith(b"command-end"))
        kernel = (output / "lima-colima-serial.log").read_text()
        self.assertNotIn("discarded-prefix", kernel)
        self.assertTrue(kernel.endswith("kernel-end"))
        self.assertEqual((output / "lima-_networks-user-v2-usernet.stderr.log").read_text(),
                         "network evidence")
        self.assertFalse((output / "lima-colima-serial0.log").exists())
        self.assertEqual(outside.read_text(), "unrelated user log")
        reports = json.loads((output / "summary.json").read_text())["commands"]
        self.assertTrue(all(report["exit_code"] == 0 for report in reports))

    def test_runtime_evidence_survives_daemon_timeout_and_missing_host_tools(self):
        output = self.area / "evidence"
        calls = []

        def run(command, **options):
            calls.append(command)
            options["stdout"].write(b"partial diagnostic")
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(command, 10)
            if len(calls) == 2:
                raise FileNotFoundError("unavailable")
            return subprocess.CompletedProcess(command, 7)

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.area)}), \
                patch.object(docker.subprocess, "run", side_effect=run):
            docker.collect_runtime_diagnostics(self.root, output)
        reports = json.loads((output / "summary.json").read_text())["commands"]
        self.assertEqual(len(reports), 8)
        self.assertEqual(reports[0]["error"], "TimeoutExpired")
        self.assertEqual(reports[1]["error"], "FileNotFoundError")
        self.assertEqual(reports[-1]["exit_code"], 7)
        self.assertEqual((output / "docker-info.log").read_bytes(), b"partial diagnostic")

    def test_runtime_evidence_rejects_paths_outside_job_and_escaped_lima_storage(self):
        output = self.area / "evidence"
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.area)}), \
                patch.object(docker.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "job-owned"):
                docker.collect_runtime_diagnostics(self.area.parent / "unrelated", output)
            with self.assertRaisesRegex(ValueError, "job-owned"):
                docker.collect_runtime_diagnostics(self.root, self.area.parent / "unrelated")
            self.assertFalse(output.exists())
            outside = self.area / "user-lima"
            outside.mkdir()
            (self.root / "colima").mkdir(parents=True)
            (self.root / "colima/_lima").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "outside"):
                docker.collect_runtime_diagnostics(self.root, output)
            run.assert_not_called()
        self.assertFalse(output.exists())

    def test_workflow_uses_pinned_installer_only_for_existing_intel_cloud_lane(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ios_ci.yaml").read_text())
        steps = workflow["jobs"]["integration-tests"]["steps"]
        setup = next(step for step in steps if step.get("name") == "Setup Docker on macOS")
        self.assertEqual(setup["uses"], "./.mobile-ci-tools/.github/actions/macos-docker")
        self.assertIn("matrix.test_number == 4", setup["if"])
        self.assertEqual(setup["timeout-minutes"], 15)
        action = yaml.safe_load((ROOT / ".github/actions/macos-docker/action.yml").read_text())
        commands = [step.get("run", "") for step in action["runs"]["steps"]]
        self.assertTrue(any("macos_docker.py\" install" in command for command in commands))
        self.assertTrue(any("macos_docker.py\" start" in command for command in commands))
        self.assertFalse(any("brew install" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
