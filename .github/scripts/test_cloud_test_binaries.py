"""Exercise the archive handoff with real Rust binaries, without downloading dependencies."""

import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

import cloud_test_binaries as binaries


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / ".github/scripts/cloud_test_binaries.py"
FEATURES = "ai-test-enabled,sync-v2,ci-test"
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/cloud_integration_ci.yaml").read_text())


def step(job, name):
    return next(item for item in WORKFLOW["jobs"][job]["steps"] if item.get("name") == name)


@unittest.skipUnless(shutil.which("cargo") and shutil.which("cc"), "requires Rust and a C compiler")
class CloudTestArchiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="cloud-test-archive-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.workspace = cls.root / "source"
        cls.workspace.mkdir()
        files = {
            "Cargo.toml": '''[package]
name = "appflowy-cloud"
version = "0.1.0"
edition = "2021"
[[bin]]
name = "appflowy_cloud"
path = "src/main.rs"
[features]
ai-test-enabled = []
sync-v2 = []
ci-test = []
''',
            "build.rs": '''use std::{env, fs, process::Command};
fn main() {
    let out = env::var("OUT_DIR").unwrap();
    fs::write(format!("{out}/fixture.txt"), "runtime fixture").unwrap();
    let target = env::var("TARGET").unwrap().replace('-', "_");
    let compiler = env::var(format!("CC_{target}")).unwrap_or_else(|_| "cc".to_owned());
    assert!(Command::new(compiler).args(["-shared", "-fPIC", "native.c", "-o",
        &format!("{out}/libfixture.so")]).status().unwrap().success());
    println!("cargo:rustc-link-search=native={out}");
    println!("cargo:rustc-link-lib=dylib=fixture");
}
''',
            "native.c": "int fixture_value(void) { return 7; }\n",
            "src/lib.rs": '''extern "C" { fn fixture_value() -> i32; }
pub fn value() -> i32 { unsafe { fixture_value() } }
#[test]
fn library_unit() { assert_eq!(value(), 7); }
''',
            "src/main.rs": '''fn main() { println!("helper-binary:{}", appflowy_cloud::value()); }
#[test]
fn binary_unit() { assert_eq!(appflowy_cloud::value(), 7); }
''',
            "tests/main.rs": '''mod workspace {
    #[test]
    fn selected() {
        assert_eq!(appflowy_cloud::value(), 7);
        assert_eq!(std::env::current_dir().unwrap().to_str().unwrap(), env!("CARGO_MANIFEST_DIR"));
        assert_eq!(std::env::var("CARGO_MANIFEST_DIR").unwrap(), env!("CARGO_MANIFEST_DIR"));
        assert_eq!(std::fs::read_to_string(concat!(env!("OUT_DIR"), "/fixture.txt")).unwrap(),
                   "runtime fixture");
        let output = std::process::Command::new(env!("CARGO_BIN_EXE_appflowy_cloud"))
            .output().unwrap();
        assert!(output.status.success());
        assert_eq!(String::from_utf8(output.stdout).unwrap().trim(), "helper-binary:7");
    }
    #[test]
    fn skipped_case() { panic!("the workflow should skip this test"); }
}
mod failure {
    #[test]
    fn fails() { panic!("test failures must propagate"); }
}
mod database {
    mod database_index_test {
        #[test]
        fn selected() { std::fs::write("index-first-ran", "ready").unwrap(); }
    }
    #[test]
    fn history() { panic!("database history must keep its own stack"); }
}
mod search {
    #[test]
    fn selected() {
        assert_eq!(std::fs::read_to_string("index-first-ran").unwrap(), "ready");
    }
}
mod user { mod delete {
    #[test]
    #[ignore]
    fn ci_final_delete_eva() {
        assert_eq!(std::env::var("CI").unwrap(), "true");
        assert_eq!(std::env::var("APPFLOWY_CI_FINAL_ACCOUNT_DELETE").unwrap(), "true");
        std::fs::write("final-test-ran", "yes").unwrap();
    }
}}
''',
            "tests/separate.rs": "#[test]\nfn separate_target() {}\n",
            ".gitignore": "target/\nci-tools\nfinal-test-ran\nindex-first-ran\n",
            # Model the source-owned script's guard, exact listing and final execution.
            "script/test_final_account_deletion.sh": '''set -euo pipefail
if [[ "${CI:-}" != true ]]; then
  echo "Final account deletion requires CI=true" >&2
  exit 1
fi
test_name=user::delete::ci_final_delete_eva
listing=$(cargo test --locked --test main "$@" "$test_name" -- --ignored --exact --list)
awk -v name="$test_name" '$0 == name ": test" { found++ } END { exit found != 1 }' <<< "$listing"
APPFLOWY_CI_FINAL_ACCOUNT_DELETE=true cargo test --locked --test main "$@" "$test_name" \\
  -- --ignored --exact --test-threads=1 --nocapture
''',
        }
        for name, content in files.items():
            path = cls.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        cls.build_env = {**os.environ, "RUSTFLAGS": "-C prefer-dynamic", "RUSTC_WRAPPER": "",
                         "CARGO_INCREMENTAL": "0", "CARGO_TARGET_DIR": str(cls.workspace / "target")}
        cls.call(["cargo", "generate-lockfile", "--offline"], env=cls.build_env)
        cls.call(["git", "init", "-q"])
        cls.call(["git", "add", "."])
        cls.call(["git", "-c", "user.name=CI fixture", "-c", "user.email=ci@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "Test fixture"])
        cls.archive = cls.root / "tests.tar.gz"
        cls.helper("build", "--features", FEATURES, "--archive", str(cls.archive), env=cls.build_env)
        # Erase all compiler output: the consumer must work with the archive alone.
        shutil.rmtree(cls.workspace / "target")
        cls.helper("restore", "--archive", str(cls.archive))
        cls.manifest = json.loads((cls.workspace / binaries.MANIFEST).read_text())
        (cls.workspace / "ci-tools").symlink_to(REPO, target_is_directory=True)
        cls.no_compilers = cls.root / "no-compilers"
        cls.no_compilers.mkdir()
        for name in ("cargo", "rustc"):
            executable = cls.no_compilers / name
            executable.write_text("#!/bin/sh\necho 'Unexpected compilation' >&2\nexit 97\n")
            executable.chmod(0o755)
        cls.run_env = {**os.environ, "PATH": f'{cls.no_compilers}:{os.environ["PATH"]}',
                       "GITHUB_WORKSPACE": str(cls.workspace), "CLOUD_TEST_FEATURES": FEATURES,
                       "CLOUD_CACHE_GROUP": "root", "CI": "true"}

    @classmethod
    def call(cls, command, *, env=None, check=True):
        result = subprocess.run(command, cwd=cls.workspace, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if check and result.returncode:
            raise AssertionError(f"{command} failed ({result.returncode}):\n{result.stdout}")
        return result

    @classmethod
    def helper(cls, *args, env=None, check=True):
        return cls.call([sys.executable, str(HELPER), *args], env=env, check=check)

    def run_tests(self, *args, check=True):
        return self.helper("run", "test", "--locked", "--features", FEATURES, *args,
                           env=self.run_env, check=check)

    def test_archive_contains_runtime_dependencies_without_compiler_intermediates(self):
        with tarfile.open(self.archive) as archive:
            names = archive.getnames()
        self.assertIn("target/debug/appflowy_cloud", names)
        self.assertTrue(any(name.endswith("libfixture.so") for name in names))
        self.assertTrue(any(name.endswith("fixture.txt") for name in names))
        self.assertTrue(any(name.startswith("target/cloud-test-runtime/libstd-") for name in names))
        self.assertFalse(any(name.endswith((".rlib", ".rmeta", ".o")) for name in names))
        self.assertEqual(len(self.manifest["tests"]), 4)  # lib, bin and two integration targets

    def test_module_filters_skips_assets_and_helper_binary_without_cargo(self):
        result = self.run_tests("workspace::", "--", "--skip", "skipped_case", "--test-threads=1")
        self.assertIn("test workspace::selected ... ok", result.stdout)
        self.assertNotIn("test workspace::skipped_case ...", result.stdout)

    def test_root_unit_selection_excludes_integration_tests(self):
        result = self.run_tests("--lib", "--bins", "--", "--test-threads=1")
        self.assertIn("test library_unit ... ok", result.stdout)
        self.assertIn("test binary_unit ... ok", result.stdout)
        self.assertNotIn("Running precompiled main", result.stdout)

    def test_target_runtime_verification_only_lists_tests(self):
        marker = self.workspace / "final-test-ran"
        marker.unlink(missing_ok=True)
        result = self.helper("verify", "--archive", str(self.archive), "--source-sha",
                             self.manifest["source_sha"], env=self.run_env)
        self.assertIn("Verified startup of 4 test executables", result.stdout)
        self.assertFalse(marker.exists())
        result = self.helper("verify", "--source-sha", self.manifest["source_sha"], env=self.run_env)
        self.assertIn("Verified startup of 4 test executables", result.stdout)
        self.assertFalse(marker.exists())

    def test_explicit_standalone_target(self):
        result = self.run_tests("--test", "separate", "--", "--test-threads=1")
        self.assertIn("test separate_target ... ok", result.stdout)
        self.assertNotIn("Running precompiled main", result.stdout)

    def test_libtest_failure_is_not_swallowed(self):
        result = self.run_tests("--test", "main", "failure::", "--", "--test-threads=1", check=False)
        self.assertEqual(result.returncode, 101, result.stdout)

    def test_invalid_selection_or_features_fails_instead_of_compiling(self):
        for extra in (("--test", "missing"), ("--features", "different"), ("--release",)):
            with self.subTest(extra=extra):
                result = self.run_tests(*extra, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Unexpected compilation", result.stdout)
                self.assertNotIn("Running precompiled", result.stdout)

    def test_final_cleanup_keeps_source_guard_and_exact_ignored_test(self):
        command = ["bash", "-e", "-o", "pipefail", "-c",
                   step("test", "Final seeded account deletion")["run"]]
        marker = self.workspace / "final-test-ran"
        marker.unlink(missing_ok=True)
        rejected = self.call(command, env={**self.run_env, "CI": "false"}, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(marker.exists())
        listing = self.run_tests("--test", "main", "user::delete::ci_final_delete_eva", "--",
                                 "--ignored", "--exact", "--list")
        self.assertEqual(listing.stdout.count("user::delete::ci_final_delete_eva: test"), 1)
        self.assertFalse(marker.exists())
        result = self.call(command, env=self.run_env)
        self.assertIn("test user::delete::ci_final_delete_eva ... ok", result.stdout)
        self.assertEqual(marker.read_text(), "yes")

    def test_workflow_runs_combined_unit_and_module_tests_from_archive(self):
        for service, root_units in (
            ("appflowy_cloud_workspace_core", False),
            ("appflowy_cloud_core", True),
        ):
            with self.subTest(service=service):
                script = step("test", "Run Tests")["run"]
                script = script.replace("${{ matrix.test_service }}", service)
                script = script.replace("${{ matrix.test_modules }}", "workspace")
                result = self.call(["bash", "-e", "-o", "pipefail", "-c", script],
                                   env={**self.run_env, "TEST_SKIPS": "skipped_case",
                                        "RUN_ROOT_UNIT_TESTS": str(root_units).lower()})
                self.assertEqual(result.stdout.count("test workspace::selected ... ok"), 1)
                self.assertNotIn("test workspace::skipped_case ...", result.stdout)
                for test in ("library_unit", "binary_unit"):
                    output = f"test {test} ... ok"
                    self.assertEqual(result.stdout.count(output), int(root_units))
                    if root_units:
                        self.assertLess(result.stdout.index(output),
                                        result.stdout.index("test workspace::selected ... ok"))

    def test_combined_lane_propagates_integration_failure_after_passing_units(self):
        script = step("test", "Run Tests")["run"]
        script = script.replace("${{ matrix.test_service }}", "appflowy_cloud_core")
        script = script.replace("${{ matrix.test_modules }}", "failure")
        result = self.call(["bash", "-e", "-o", "pipefail", "-c", script],
                           env={**self.run_env, "RUN_ROOT_UNIT_TESTS": "true"}, check=False)
        self.assertIn("test library_unit ... ok", result.stdout)
        self.assertIn("test failure::fails ... FAILED", result.stdout)
        self.assertEqual(result.returncode, 101, result.stdout)

    def test_search_topic_indexes_first_without_running_database_history(self):
        lane = next(lane for lane in WORKFLOW["jobs"]["test"]["strategy"]["matrix"]["include"]
                    if lane["test_service"] == "appflowy_cloud_search")
        script = step("test", "Run Tests")["run"]
        script = script.replace("${{ matrix.test_service }}", lane["test_service"])
        script = script.replace("${{ matrix.test_modules }}", lane["test_modules"])
        marker = self.workspace / "index-first-ran"
        marker.unlink(missing_ok=True)
        self.addCleanup(marker.unlink, missing_ok=True)
        result = self.call(["bash", "-e", "-o", "pipefail", "-c", script],
                           env={**self.run_env, "RUN_ROOT_UNIT_TESTS": "false",
                                "TEST_SKIPS": lane.get("test_skips", "")})
        self.assertIn("test database::database_index_test::selected ... ok", result.stdout)
        self.assertIn("test search::selected ... ok", result.stdout)
        self.assertNotIn("test database::history ...", result.stdout)

    def test_manifest_must_match_source_path_os_architecture_and_version(self):
        for key, value in (("source_sha", "wrong"), ("workspace", "/different/path"),
                           ("host", ["Linux", "other-arch", "ubuntu", "24.04"]), ("version", 2)):
            with self.subTest(key=key):
                manifest = {**self.manifest, key: value}
                with patch.object(binaries, "command_output", return_value=self.manifest["source_sha"]):
                    with self.assertRaises(ValueError):
                        binaries.validate_manifest(manifest, self.workspace)

    def test_restore_rejects_unsafe_or_incomplete_archive_before_extraction(self):
        for attack in ("traversal", "symlink", "duplicate", "missing-executable", "wrong-source"):
            with self.subTest(attack=attack):
                archive_path = self.root / f"{attack}.tar.gz"
                manifest = copy.deepcopy(self.manifest)
                if attack == "wrong-source":
                    manifest["source_sha"] = "wrong"
                with tarfile.open(archive_path, "w:gz") as archive:
                    content = json.dumps(manifest).encode()
                    info = tarfile.TarInfo(str(binaries.MANIFEST))
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                    sentinel = tarfile.TarInfo(f"target/{attack}-must-not-exist")
                    archive.addfile(sentinel)
                    if attack == "traversal":
                        archive.addfile(tarfile.TarInfo("target/../../escape"))
                    elif attack == "symlink":
                        link = tarfile.TarInfo("target/escape-link")
                        link.type, link.linkname = tarfile.SYMTYPE, "/tmp"
                        archive.addfile(link)
                    elif attack == "duplicate":
                        archive.addfile(sentinel)
                result = self.helper("restore", "--archive", str(archive_path), check=False)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((self.workspace / sentinel.name).exists())

    def test_existing_symlink_cannot_escape_target(self):
        link = self.workspace / "target/external"
        link.symlink_to(self.root, target_is_directory=True)
        try:
            with self.assertRaises(ValueError):
                binaries.target_path("target/external/escaped", self.workspace)
        finally:
            link.unlink()


class SharedBuildWorkflowTest(unittest.TestCase):
    def test_unfiltered_root_units_run_once_alongside_integration_modules(self):
        lanes = WORKFLOW["jobs"]["test"]["strategy"]["matrix"]["include"]
        unit_lanes = [lane for lane in lanes if lane.get("test_root_unit")]
        self.assertEqual(len(unit_lanes), 1)
        self.assertTrue(unit_lanes[0]["test_modules"].split())
        self.assertNotIn("cache_group", unit_lanes[0])
        self.assertEqual(step("test", "Run Tests")["env"]["RUN_ROOT_UNIT_TESTS"],
                         "${{ matrix.test_root_unit == true }}")

    def matrix_will_run(self, builder, **results):
        needs = {}
        for job in WORKFLOW["jobs"]["test"]["needs"]:
            unused = ((job == "build_self_hosted" and builder == "github-hosted")
                      or (job.startswith("build_") and job != "build_self_hosted"
                          and builder == "self-hosted"))
            needs[job] = SimpleNamespace(result=results.get(job, "skipped" if unused else "success"))
        needs["image_source"].outputs = SimpleNamespace(builder=builder)
        expression = WORKFLOW["jobs"]["test"]["if"]
        expression = expression.replace("always()", "True").replace("!cancelled()", "True")
        expression = expression.replace("&&", "and").replace("||", "or")
        return eval(expression, {"__builtins__": {}}, {"needs": SimpleNamespace(**needs)})

    def test_unused_github_compilation_does_not_skip_self_hosted_tests(self):
        self.assertTrue(self.matrix_will_run("self-hosted"))
        self.assertTrue(self.matrix_will_run("github-hosted"))
        for builder, job in (("self-hosted", "build_self_hosted"),
                             ("github-hosted", "build_test_binaries"),
                             ("github-hosted", "build_cloud")):
            for result in ("failure", "skipped", "cancelled"):
                with self.subTest(builder=builder, job=job, result=result):
                    self.assertFalse(self.matrix_will_run(builder, **{job: result}))

    def test_shared_compilation_can_overlap_image_builds(self):
        jobs = WORKFLOW["jobs"]
        self.assertEqual(jobs["build_test_binaries"]["needs"], "image_source")
        self.assertEqual(jobs["build_test_binaries"]["runs-on"], jobs["test"]["runs-on"])
        self.assertEqual(jobs["test"]["runs-on"], "ubuntu-24.04")
        self.assertIn("build_test_binaries", jobs["test"]["needs"])
        self.assertIn("needs.build_test_binaries.result == 'success'", jobs["test"]["if"])
        self.assertEqual(jobs["build_test_binaries"]["if"],
                         "needs.image_source.outputs.builder == 'github-hosted'")

    def test_consumers_download_the_producers_source_specific_artifact(self):
        upload = step("build_test_binaries", "Upload shared Cloud test binaries")["with"]
        download = step("test", "Download shared Cloud test binaries")["with"]
        self.assertEqual(upload["name"], download["name"])
        self.assertIn("needs.image_source.outputs.sha", upload["name"])
        self.assertTrue(upload["overwrite"])  # A full rerun replaces this run's archive.
        images = step("test", "Download Docker Images")["with"]
        for key in ("repository", "run-id", "github-token"):
            self.assertEqual(download[key], images[key])

    def test_cross_build_rejects_host_architecture_executables(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            header = bytearray(20)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = b"\x3e\x00"
            binary.write_bytes(header)
            binaries.verify_amd64_executable(binary)
            header[18:20] = b"\xb7\x00"  # EM_AARCH64
            binary.write_bytes(header)
            with self.assertRaisesRegex(ValueError, "AMD64 Linux"):
                binaries.verify_amd64_executable(binary)

    def test_docker_source_revision_requires_an_immutable_commit(self):
        for revision in ("main", "a" * 39, "b" * 40 + "\n"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                binaries.source_revision(revision)

    def test_package_lanes_still_use_cargo_and_their_original_package_selections(self):
        lanes = [lane for lane in WORKFLOW["jobs"]["test"]["strategy"]["matrix"]["include"]
                 if lane.get("cache_group")]
        self.assertEqual({lane["cache_group"] for lane in lanes}, {"worker", "search", "members"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cargo = root / "cargo"
            cargo.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            cargo.chmod(0o755)
            for lane in lanes:
                with self.subTest(service=lane["test_service"]):
                    script = step("test", "Run Tests")["run"]
                    script = script.replace("${{ matrix.test_service }}", lane["test_service"])
                    script = script.replace("${{ matrix.test_modules }}", lane["test_modules"])
                    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                                            env={**os.environ, "PATH": f'{root}:{os.environ["PATH"]}',
                                                 "CLOUD_CACHE_GROUP": lane["cache_group"]},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected = {"worker": "appflowy-worker", "search": "appflowy-search",
                                "members": "workspace-folder"}[lane["cache_group"]]
                    self.assertIn(f"-p\n{expected}\n", result.stdout)
                    self.assertIn("--test-threads=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
