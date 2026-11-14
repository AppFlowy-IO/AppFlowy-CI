"""Exercise the media runtime build without compiling or installing host packages."""

import difflib
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

import prepare_linux_mpv as runtime


ROOT = Path(__file__).resolve().parents[2]
RUN = subprocess.run


class PrepareLinuxMpvTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.area = Path(self.directory.name)
        self.root = self.area / "runtime"
        self.commands = []
        self.source_name = f"mpv-{runtime.SOURCE_COMMIT}/audio/out/ao_pipewire.c"
        self.archive = self.area / "source.tar.gz"
        self.make_archive({self.source_name: b"start()\nconnect()\n"})
        self.patch_file = self.area / "startup.patch"
        self.patch_file.write_text("".join(difflib.unified_diff(
            ["start()\n", "connect()\n"], ["connect()\n", "start()\n"],
            fromfile="a/audio/out/ao_pipewire.c", tofile="b/audio/out/ao_pipewire.c",
        )))
        self.assets = [
            {**runtime.ASSETS[0], "sha256": runtime.checksum(self.archive)},
            {**runtime.ASSETS[1], "sha256": runtime.checksum(self.patch_file)},
        ]
        self.asset_patch = patch.object(runtime, "ASSETS", self.assets)
        self.asset_patch.start()
        self.addCleanup(self.asset_patch.stop)

    def make_archive(self, files):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, content in files.items():
                item = tarfile.TarInfo(name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))

    def fetch(self, asset, directory):
        self.assertEqual(directory, self.root / "downloads")
        return self.archive if asset is self.assets[0] else self.patch_file

    def run_command(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[0] == "patch":
            return RUN(command, **kwargs)
        if command[0] == "meson":
            source = Path(command[3])
            self.assertEqual((source / "audio/out/ao_pipewire.c").read_text(),
                             "connect()\nstart()\n")
        if command[0] == "ninja" and command[-1] == "install":
            library = self.root / "install/lib/libmpv.so.2"
            library.parent.mkdir(parents=True)
            library.write_bytes(b"\x7fELFtest library")
        return subprocess.CompletedProcess(command, 0, "libc.so => /lib/libc.so\n", "")

    def build(self):
        with patch.object(runtime.subprocess, "run", side_effect=self.run_command):
            return runtime.build_runtime(self.root, self.fetch)

    def test_builds_verified_source_with_only_the_upstream_patch(self):
        result = self.build()
        self.assertEqual(result["source_commit"], runtime.SOURCE_COMMIT)
        self.assertEqual(result["upstream_fix"], runtime.FIX_COMMIT)
        self.assertEqual(json.loads((self.root / "manifest.json").read_text()), result)
        self.assertEqual(result["library_sha256"], runtime.checksum(Path(result["library"])))
        commands = [command for command, _ in self.commands]
        self.assertEqual([command[0] for command in commands],
                         ["patch", "meson", "ninja", "ninja", "ldd"])
        self.assertIn("--fuzz=0", commands[0])
        self.assertIn(f"--prefix={self.root}/install", commands[1])
        self.assertIn("-Dlibmpv=true", commands[1])
        self.assertFalse(any("pipewire=disabled" in item or "audio=disabled" in item
                             for command in commands for item in command))
        self.assertEqual([kwargs["timeout"] for _, kwargs in self.commands],
                         [30, 120, 420, 60, 30])
        self.assertFalse(list(self.root.glob("source-*")))

    def test_checksum_failure_cannot_reuse_a_previous_success_manifest(self):
        self.root.mkdir()
        (self.root / "manifest.json").write_text('{"old":"success"}')
        self.patch_file.write_text("corrupt patch")
        with patch.object(runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                runtime.build_runtime(self.root, self.fetch)
            run.assert_not_called()
        self.assertFalse((self.root / "manifest.json").exists())

    def test_rejects_archive_paths_outside_the_build_directory(self):
        self.make_archive({"../../escaped": b"outside"})
        self.assets[0]["sha256"] = runtime.checksum(self.archive)
        with patch.object(runtime.subprocess, "run") as run:
            with self.assertRaises(tarfile.FilterError):
                runtime.build_runtime(self.root, self.fetch)
            run.assert_not_called()
        self.assertFalse((self.area / "escaped").exists())

    def test_rejects_an_install_symlink_outside_the_owned_directory(self):
        self.root.mkdir()
        outside = self.area / "outside"
        outside.mkdir()
        (outside / "keep").write_text("unchanged")
        (self.root / "install").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            runtime.build_runtime(self.root, self.fetch)
        self.assertEqual((outside / "keep").read_text(), "unchanged")

    def test_a_rejected_patch_stops_before_any_build(self):
        self.make_archive({self.source_name: b"different source\n"})
        self.assets[0]["sha256"] = runtime.checksum(self.archive)
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        self.assertEqual(len(self.commands), 1)
        self.assertFalse((self.root / "manifest.json").exists())

    def test_compiler_failure_preserves_its_log_and_has_no_success_manifest(self):
        def fail_build(command, **kwargs):
            if command[0] == "ninja":
                kwargs["stdout"].write("compiler failed\n")
                raise subprocess.CalledProcessError(1, command)
            return self.run_command(command, **kwargs)
        with patch.object(runtime.subprocess, "run", side_effect=fail_build):
            with self.assertRaises(subprocess.CalledProcessError):
                runtime.build_runtime(self.root, self.fetch)
        self.assertIn("compiler failed", (self.root / "build.log").read_text())
        self.assertFalse((self.root / "manifest.json").exists())

    def test_missing_or_invalid_library_prevents_success(self):
        for content in (None, b"not an ELF"):
            with self.subTest(content=content):
                def invalid_install(command, **kwargs):
                    result = self.run_command(command, **kwargs)
                    if command[0] == "ninja" and command[-1] == "install":
                        library = self.root / "install/lib/libmpv.so.2"
                        if content is None:
                            library.unlink()
                        else:
                            library.write_bytes(content)
                    return result
                with patch.object(runtime.subprocess, "run", side_effect=invalid_install):
                    with self.assertRaises((FileNotFoundError, RuntimeError)):
                        runtime.build_runtime(self.root, self.fetch)
                self.assertFalse((self.root / "manifest.json").exists())

    def test_missing_runtime_dependency_prevents_success(self):
        def unresolved(command, **kwargs):
            if command[0] == "ldd":
                return subprocess.CompletedProcess(command, 0, "libavcodec.so => not found\n", "")
            return self.run_command(command, **kwargs)
        with patch.object(runtime.subprocess, "run", side_effect=unresolved):
            with self.assertRaisesRegex(RuntimeError, "unresolved runtime dependencies"):
                runtime.build_runtime(self.root, self.fetch)
        self.assertFalse((self.root / "manifest.json").exists())

    def test_retry_replaces_only_its_own_old_installation(self):
        stale = self.root / "install/lib/old-library"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale")
        self.build()
        self.assertFalse(stale.exists())
        self.assertTrue((self.root / "install/lib/libmpv.so.2").exists())

    def test_cli_rejects_the_shared_temp_root_and_paths_outside_it(self):
        for directory in (self.area, self.area.parent / "outside-runtime"):
            with self.subTest(directory=directory), \
                    patch.dict(os.environ, {"RUNNER_TEMP": str(self.area)}), \
                    patch.object(sys, "argv", ["prepare_linux_mpv.py", "--directory", str(directory)]), \
                    patch.object(runtime, "build_runtime") as build:
                with self.assertRaises(ValueError):
                    runtime.main()
                build.assert_not_called()

    def test_workflow_builds_only_for_the_source_owned_media_runner(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/flutter_ci.yaml").read_text())
        job = workflow["jobs"]["integration_test"]
        step = next(item for item in job["steps"]
                    if item.get("name") == "Build fixed libmpv for media integration tests")
        self.assertEqual(step["timeout-minutes"], 10)
        self.assertEqual(step["working-directory"], "frontend/appflowy_flutter")
        self.assertIn("desktop/document/document_media_test_runner.dart", step["run"])
        self.assertIn("prepare_linux_mpv.py", step["run"])
        self.assertNotIn("LIBMPV_LIBRARY_PATH=", step["run"])
        self.assertNotIn("LD_LIBRARY_PATH=", step["run"])
        self.assertIn("PKG_CONFIG_PATH=", step["run"])
        self.assertIn("rm -f build/linux/x64/debug/CMakeCache.txt", step["run"])
        self.assertIn("GITHUB_ENV", step["run"])
        test = next(item for item in job["steps"] if item.get("name") == "Run Flutter integration tests")
        self.assertEqual(test["timeout-minutes"], 45)
        upload = next(item for item in job["steps"] if item.get("name") == "Upload desktop test output")
        self.assertIn("linux-media-runtime/build.log", upload["with"]["path"])
        self.assertIn("linux-media-runtime/manifest.json", upload["with"]["path"])

    def test_workflow_selects_media_and_publishes_only_a_successful_runtime(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/flutter_ci.yaml").read_text())
        step = next(item for item in workflow["jobs"]["integration_test"]["steps"]
                    if item.get("name") == "Build fixed libmpv for media integration tests")
        verify = next(item for item in workflow["jobs"]["integration_test"]["steps"]
                      if item.get("name") == "Verify bundled media runtime")
        self.assertEqual(verify["if"], "always()")
        self.assertEqual(verify["timeout-minutes"], 1)
        command = step["run"].replace("${{ matrix.test_number }}", "26")
        for media, failed in ((True, False), (False, False), (True, True)):
            with self.subTest(media=media, failed=failed):
                root = self.area / f"workflow-{media}-{failed}"
                app = root / "frontend/appflowy_flutter"
                tests = app / "integration_test"
                tests.mkdir(parents=True)
                (tests / "desktop_runner_26.dart").write_text(
                    "import 'desktop/document/document_media_test_runner.dart';\n"
                    if media else "import 'desktop/document/document_test_runner.dart';\n",
                )
                cache = app / "build/linux/x64/debug/CMakeCache.txt"
                cache.parent.mkdir(parents=True)
                cache.write_text("cached system libmpv")
                tools = root / "bin"
                tools.mkdir()
                python = tools / "python3"
                python.write_text(
                    f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                    "with Path(os.environ['CALLS']).open('a') as output:\n"
                    " output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "if sys.argv[1].endswith('prepare_linux_mpv.py'):\n"
                    " sys.exit(7 if os.environ['FAIL_BUILD'] == 'true' else 0)\n",
                )
                python.chmod(0o700)
                environment = {
                    **os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                    "GITHUB_WORKSPACE": str(root), "RUNNER_TEMP": str(root / "runner"),
                    "GITHUB_ENV": str(root / "github-env"), "CALLS": str(root / "calls"),
                    "FAIL_BUILD": str(failed).lower(), "LD_LIBRARY_PATH": "/previous/libs",
                    "PKG_CONFIG_PATH": "/previous/pkgconfig",
                }
                result = RUN(["bash", "-c", command], cwd=app, env=environment,
                             text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 7 if failed else 0, result.stderr)
                calls = root / "calls"
                recorded = [json.loads(line) for line in calls.read_text().splitlines()] \
                    if calls.exists() else []
                self.assertEqual(len(recorded), 2 if media else 0)
                exported = root / "github-env"
                if media and not failed:
                    library_dir = root / "runner/linux-media-runtime/install/lib"
                    self.assertEqual(exported.read_text().splitlines(), [
                        f"PKG_CONFIG_PATH={library_dir}/pkgconfig:/previous/pkgconfig",
                    ])
                    self.assertFalse(cache.exists())
                    manifest = root / "runner/linux-media-runtime/manifest.json"
                    manifest.parent.mkdir(parents=True)
                    manifest.write_text(json.dumps({"library_sha256": runtime.checksum(self.archive)}))
                    library = app / "build/linux/x64/debug/bundle/lib/libmpv.so.2"
                    library.parent.mkdir(parents=True)
                    library.write_bytes(self.archive.read_bytes())
                    library.with_name("libmpv.so").symlink_to(library.name)
                    verify_environment = {**environment, "PATH": os.environ["PATH"]}
                    verified = RUN(["bash", "-c", verify["run"]], cwd=app,
                                   env=verify_environment, text=True, capture_output=True, timeout=15)
                    self.assertEqual(verified.returncode, 0, verified.stderr)
                    self.assertEqual(json.loads(manifest.read_text())["bundled_library_sha256"],
                                     runtime.checksum(self.archive))
                    library.write_bytes(b"stale system library")
                    rejected = RUN(["bash", "-c", verify["run"]], cwd=app,
                                   env=verify_environment, text=True, capture_output=True, timeout=15)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("The app bundled a different libmpv", rejected.stderr)
                else:
                    self.assertFalse(exported.exists())
                    self.assertEqual(cache.read_text(), "cached system libmpv")
                    verified = RUN(["bash", "-c", verify["run"]], cwd=app,
                                   env={**environment, "PATH": os.environ["PATH"]},
                                   text=True, capture_output=True, timeout=15)
                    self.assertEqual(verified.returncode, 0, verified.stderr)
