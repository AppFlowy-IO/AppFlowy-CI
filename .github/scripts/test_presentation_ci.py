"""Presentation coverage must execute bodies, keep owner graphs, and fail missing fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import presentation_acceptance_runner as acceptance
import presentation_test_runner as runner
import cloud_test_binaries as archive


REPO = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/cloud_integration_ci.yaml").read_text())


def completed(args, output=""):
    return subprocess.CompletedProcess(args, 0, output, "")


def local_run(args, *, environment=None, capture_output=False, inherit_stderr=False,
              cwd=None, check=False, timeout):
    return subprocess.run(args, cwd=cwd, env=environment, text=True,
                          stdout=subprocess.PIPE if capture_output else None,
                          stderr=None if inherit_stderr else subprocess.PIPE,
                          check=check, timeout=timeout)


class CoverageGuardsTest(unittest.TestCase):
    def test_production_source_selects_required_followup_contracts_without_mutating_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Path(directory)
            binaries = [runner.Binary(owner / "libtest", owner)]
            package = "appflowy-cloud-ai"
            baseline = runner.POLICIES[package]
            original = json.loads(json.dumps(baseline))
            self.assertEqual(runner.policy_for_source(package, binaries), baseline)
            update = baseline["source_updates"][0]
            marker = owner / update["when_file"]
            marker.parent.mkdir(parents=True)
            marker.write_text("// production read authority exists\n")
            current = runner.policy_for_source(package, binaries)
            self.assertEqual(len(current["database"]), 77)
            self.assertTrue(set(update["database"]).issubset(current["database"]))
            self.assertFalse(set(update["remove_database"]) & set(current["database"]))
            self.assertEqual(runner.POLICIES[package], original)
            # The production marker stays present if a regression body disappears.
            with self.assertRaises(RuntimeError):
                runner.require_tests("followup", set(current["database"]) - {update["database"][0]},
                                     len(current["database"]), required=current["database"])

    def test_discovery_requires_counts_and_each_required_name(self):
        for selected, minimum, required in [(set(), 1, ()), ({"a"}, 2, ()), ({"a"}, 1, ("b",))]:
            with self.subTest(selected=selected), self.assertRaises(RuntimeError):
                runner.require_tests("fixture", selected, minimum, required=required)
        runner.require_tests("fixture", {"a", "b"}, 2, required=["a"])

    def test_execution_cannot_pass_zero_or_ignored_tests(self):
        for output in ("", "test result: ok. 0 passed; 0 failed; 1 ignored;",
                       "test result: ok. 0 passed; 0 failed; 0 ignored;",
                       "test result: ok. 1 passed; 1 failed; 0 ignored;"):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                runner.require_passed(output, 1)
        runner.require_passed("test result: ok. 1 passed; 0 failed; 0 ignored;", 1)

    def exercise(self, *, missing=(), extra=(), zero_execution=False, crash=False):
        policy = {"ordinary_minimum": 1, "database": ["db"], "media": ["media"],
                  "acceptance": ["native", "gateway"], "external": ["provider"]}
        ignored = (set(policy["database"] + policy["media"] + policy["acceptance"] + policy["external"])
                   - set(missing)) | set(extra)
        executions = []

        def run(args, **kwargs):
            if "--list" in args:
                if "--exact" in args:
                    names = {args[1]} & ignored
                elif "--ignored" in args:
                    names = ignored
                elif "--skip" in args:
                    names = {"unit"}
                else:
                    names = ignored | {"unit"}
                return completed(args, "\n".join(name + ": test" for name in sorted(names)))
            executions.append(args)
            if crash:
                raise subprocess.CalledProcessError(42, args)
            return completed(args, f"test result: ok. {int(not zero_execution)} passed; 0 failed; 0 ignored;")

        with patch.dict(runner.POLICIES, fixture=policy):
            runner.run_contracts("fixture", [runner.Binary(Path("/libtest"), Path("/owner"))], run, {})
        return executions

    def test_only_explicit_database_and_provisioned_media_ignored_tests_execute(self):
        commands = self.exercise()
        self.assertEqual(len(commands), 3)
        ignored = [cmd for cmd in commands if "--ignored" in cmd]
        self.assertEqual({cmd[1] for cmd in ignored}, {"db", "media"})
        self.assertTrue(all("--exact" in cmd and "--test-threads=1" in cmd for cmd in ignored))

    def test_missing_and_new_unclassified_ignored_tests_fail_before_execution(self):
        for options in ({"missing": ["db"]}, {"missing": ["native"]}, {"extra": ["new_special"]}):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                self.exercise(**options)

    def test_empty_execution_and_test_process_failure_propagate(self):
        with self.assertRaises(RuntimeError):
            self.exercise(zero_execution=True)
        with self.assertRaises(subprocess.CalledProcessError):
            self.exercise(crash=True)

    def test_database_is_explicit_loopback_owned_fixture_with_all_three_dsns(self):
        dsn = "postgres://postgres:password@127.0.0.1:5432/presentation_ci"
        valid = dict(DATABASE_URL=dsn, TEST_DATABASE_URL=dsn,
                     APPFLOWY_PRESENTATION_TEST_DATABASE_URL=dsn,
                     APPFLOWY_PRESENTATION_CI_OWNED_POSTGRES="1")
        runner.check_database(valid, lambda *args, **kwargs: completed(args, "t\n"))
        for change in ({"DATABASE_URL": dsn.replace("127.0.0.1", "production")},
                       {"APPFLOWY_PRESENTATION_CI_OWNED_POSTGRES": "0"},
                       {"TEST_DATABASE_URL": ""}, {"DATABASE_URL": dsn.replace("presentation_ci", "postgres")}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                runner.check_database({**valid, **change}, lambda *a, **k: self.fail("must reject before psql"))

    def test_missing_media_tools_fail_and_installed_paths_are_passed_explicitly(self):
        with patch.object(runner.shutil, "which", return_value=None), self.assertRaises(RuntimeError):
            runner.configure_media({}, lambda *a, **k: self.fail("cannot run a missing tool"))
        environment, calls = {}, []
        with patch.object(runner.shutil, "which", side_effect=lambda name: "/usr/bin/" + name):
            runner.configure_media(environment, lambda args, **k: calls.append(args))
        self.assertEqual(len(calls), 3)
        self.assertEqual(environment["APPFLOWY_PRESENTATION_TEST_FFPROBE"], "/usr/bin/ffprobe")


class ArchiveOwnershipTest(unittest.TestCase):
    def test_generic_archive_execution_excludes_acceptance_and_explicit_run_requires_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "target").mkdir()
            manifest = {"features": [], "env": {}, "library_paths": [], "tests": [
                {"name": "main", "path": "target/main", "kind": ["test"]},
                {"name": "presentation_acceptance", "path": "target/acceptance", "kind": ["test"]},
            ]}
            (root / archive.MANIFEST).write_text(json.dumps(manifest))
            with patch.object(archive.Path, "cwd", return_value=root), \
                    patch.object(archive, "validate_manifest"), \
                    patch.object(archive.subprocess, "run", return_value=completed([])) as run:
                self.assertEqual(archive.run(["test", "--locked", "--", "--test-threads=1"]), 0)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][0], str(root / "target/main"))
                run.reset_mock()
                with self.assertRaisesRegex(ValueError, "owned fixture runner"):
                    archive.run(["test", "--test", "presentation_acceptance"])
                run.assert_not_called()
                self.assertEqual(archive.run(["test", "--test", "presentation_acceptance", "--", "--ignored"]), 0)
                self.assertEqual(run.call_args.args[0][0], str(root / "target/acceptance"))


@unittest.skipUnless(shutil.which("cargo") and shutil.which("cc"), "requires Rust and a C compiler")
class RealPackageArtifactsTest(unittest.TestCase):
    def test_real_cargo_all_targets_cwd_default_features_and_ignored_execution(self):
        with tempfile.TemporaryDirectory(prefix="presentation-package-") as directory:
            source = Path(directory).resolve()
            files = {
                "Cargo.toml": '''[workspace]
members = ["owner"]
resolver = "2"
[profile.ci]
inherits = "dev"
debug = 0
''',
                "owner/Cargo.toml": '''[package]
name = "fixture"
version = "0.1.0"
edition = "2021"
[features]
default = ["contract"]
contract = []
self-host-af = []
''',
                "owner/src/lib.rs": '''#[test]
fn ordinary() { assert!(cfg!(feature = "contract")); assert!(!cfg!(feature = "self-host-af")); }
#[test]
#[ignore]
fn owned_database_contract() { std::fs::write("ignored-body-ran", "yes").unwrap(); }
''',
                "owner/tests/contract.rs": '''#[test]
fn relative_fixture() { assert_eq!(std::fs::read_to_string("relative-fixture").unwrap(), "owned"); }
''',
                "owner/relative-fixture": "owned",
            }
            for name, content in files.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            environment = {**os.environ, "RUSTC_WRAPPER": "", "CARGO_TARGET_DIR": str(source / "target")}
            local_run(["cargo", "generate-lockfile", "--offline"], cwd=source,
                      environment=environment, check=True, timeout=60)

            def run(args, **kwargs):
                kwargs.setdefault("environment", environment)
                return local_run(args, **kwargs)

            binaries = runner.build_artifacts(source, "fixture", "ci", run)
            self.assertEqual(len(binaries), 2, "library and Cargo integration target must both run")
            self.assertTrue(all(binary.cwd == source / "owner" for binary in binaries))
            policy = {"ordinary_minimum": 2, "database": ["owned_database_contract"],
                      "media": [], "acceptance": [], "external": []}
            with patch.dict(runner.POLICIES, fixture=policy):
                runner.run_contracts("fixture", binaries, run, environment)
            self.assertEqual((source / "owner/ignored-body-ran").read_text(), "yes")


class AcceptanceTest(unittest.TestCase):
    def test_missing_service_revision_or_migrations_are_failing_prerequisites(self):
        for revision in (None, "main", "a" * 40):
            with self.subTest(revision=revision), self.assertRaises(RuntimeError):
                acceptance.prerequisites(Path("/absent-cloud"), Path("/absent-service"), revision)

    def test_zero_native_fullstack_or_gateway_execution_fails(self):
        for failed_phase in ("fullstack", "native", "gateway", None):
            def run(args, **kwargs):
                if "--list" in args:
                    names = (acceptance.ROOT_TESTS if args[0] == "/appflowy-cloud" else
                             [acceptance.GATEWAY_BRIDGE] if args[0] == "/appflowy-cloud-gateway" else
                             [*acceptance.NATIVE_TESTS, acceptance.CLOUD_BRIDGE])
                    return completed(args, "\n".join(name + ": test" for name in names))
                fullstack = "test_presentation_acceptance.py" in " ".join(args)
                phase = "fullstack" if fullstack else "native" if args[0] == "/appflowy-cloud-ai" else "gateway"
                count = 0 if phase == failed_phase else 3 if fullstack else 1
                return completed(args, f"test result: ok. {count} passed; 0 failed; 0 ignored;\n"
                                 "API02 actual Gateway router -> HostedPresentationUsage -> Cloud HTTP -> AppResponse: PASS")
            def build(source, package, *args, **kwargs):
                return [runner.Binary(Path("/" + package), Path("/owner"))]
            with patch.object(acceptance, "build_artifacts", side_effect=build), \
                    patch.object(acceptance, "check_database"):
                if failed_phase:
                    with self.assertRaises(RuntimeError):
                        acceptance.run_acceptance(Path("/cloud"), Path("/service"), "ci", run)
                else:
                    acceptance.run_acceptance(Path("/cloud"), Path("/service"), "ci", run)

    def test_new_ignored_root_acceptance_case_requires_reviewed_execution(self):
        names = [*acceptance.ROOT_TESTS, "unowned_case"]
        binary = runner.Binary(Path("/root"), Path("/owner"))
        def run(args, **kwargs):
            return completed(args, "\n".join(name + ": test" for name in names))
        with self.assertRaises(RuntimeError):
            acceptance.exact_ignored(binary, acceptance.ROOT_TESTS, run, {}, complete=True)

    def test_new_ordinary_root_acceptance_case_also_requires_an_owner_selection(self):
        def run(args, **kwargs):
            names = [*acceptance.ROOT_TESTS, *([] if "--ignored" in args else ["new_ordinary"])]
            return completed(args, "\n".join(name + ": test" for name in names))
        binary = runner.Binary(Path("/root"), Path("/owner"))
        with self.assertRaises(RuntimeError):
            acceptance.exact_ignored(binary, acceptance.ROOT_TESTS, run, {}, complete=True)

    def test_service_checkout_must_match_pinned_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("script/test_presentation_acceptance.py", "script/test_presentation_gateway_contract.py",
                         "appflowy-cloud-gateway/Cargo.toml", "appflowy-cloud-billing/migrations/placeholder",
                         "appflowy-cloud-gateway/migrations/placeholder"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            with patch.object(acceptance.subprocess, "check_output", return_value="b" * 40):
                with self.assertRaises(RuntimeError):
                    acceptance.prerequisites(root, root, "a" * 40)
            with patch.object(acceptance.subprocess, "check_output", return_value="a" * 40):
                acceptance.prerequisites(root, root, "a" * 40)


class PresentationWorkflowTest(unittest.TestCase):
    def test_four_packages_have_independent_default_feature_builds_and_owned_database(self):
        job = WORKFLOW["jobs"]["presentation-contracts"]
        self.assertEqual(set(job["strategy"]["matrix"]["package"]), set(runner.POLICIES))
        self.assertFalse(job["strategy"]["fail-fast"])
        self.assertIn("presentation-contracts", WORKFLOW["jobs"]["notify-webhook"]["needs"])
        self.assertEqual(job["services"]["postgres"]["ports"], ["5432:5432"])
        self.assertEqual(job["services"]["postgres"]["env"]["POSTGRES_DB"], "presentation_ci")
        self.assertEqual(job["env"]["DATABASE_URL"], job["env"]["TEST_DATABASE_URL"])
        checkout = next(step for step in job["steps"] if step.get("name") == "Checkout exact Cloud source")
        self.assertEqual(checkout["with"]["ref"], "${{ needs.image_source.outputs.sha }}")
        cache = next(step for step in WORKFLOW["jobs"]["test"]["steps"]
                     if step.get("id") == "cargo_cache")
        self.assertEqual(cache["with"]["cache-targets"], "${{ env.CLOUD_CACHE_GROUP != 'root' }}")

    def test_standalone_loop_routes_presentation_to_the_required_owner(self):
        job = WORKFLOW["jobs"]["test"]
        step = next(s for s in job["steps"] if s.get("name") == "Run Tests")
        script = step["run"].replace("${{ matrix.test_service }}", "standalone").replace("${{ matrix.test_modules }}", "")
        metadata = {"packages": [{"name": "appflowy-cloud", "targets": [
            {"name": "ordinary", "kind": ["test"]},
            {"name": "presentation_acceptance", "kind": ["test"]}]}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "cargo"
            fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                            f"metadata={metadata!r}\n"
                            "if sys.argv[1]=='metadata': print(json.dumps(metadata))\n"
                            "else:\n with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
            fake.chmod(0o755)
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                                    env={**os.environ, "PATH": str(root) + ":" + os.environ["PATH"],
                                         "TEST_TARGETS": "*", "CLOUD_RUN_ROOT": "false", "CALLS": str(root / "calls")},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commands = (root / "calls").read_text()
            self.assertIn('"ordinary"', commands)
            self.assertNotIn('"presentation_acceptance"', commands)

    def test_acceptance_is_required_and_unconfigured_service_cannot_skip_green(self):
        job = WORKFLOW["jobs"]["presentation-acceptance"]
        self.assertEqual(job["env"]["OWNED_TEST_TARGETS"], "presentation_acceptance")
        self.assertEqual(job["env"]["PRESENTATION_SERVICE_REPOSITORY"],
                         "${{ vars.PRESENTATION_SERVICE_REPOSITORY || 'AppFlowy-IO/AppFlowy-Cloud-Service' }}")
        self.assertEqual(job["env"]["PRESENTATION_SERVICE_SHA"],
                         "${{ vars.PRESENTATION_SERVICE_SHA || '44bcd0f16baa75d95d8d4c2ea873cd246498738a' }}")
        cache = next(step for step in job["steps"] if step.get("uses") == "Swatinem/rust-cache@v2")
        self.assertEqual(cache["with"]["key"], "presentation-acceptance-ci-${{ env.PRESENTATION_SERVICE_SHA }}")
        self.assertIn("presentation-acceptance", WORKFLOW["jobs"]["notify-webhook"]["needs"])
        script = job["steps"][0]["run"]
        env = {**os.environ, "PRESENTATION_SERVICE_REPOSITORY": "", "PRESENTATION_SERVICE_SHA": ""}
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::", result.stdout)
        env.update(PRESENTATION_SERVICE_REPOSITORY="owner/service", PRESENTATION_SERVICE_SHA="a" * 40)
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_capability_fails_and_only_true_prefeature_sources_are_exempt(self):
        script = next(s for s in WORKFLOW["jobs"]["verify_test_module_coverage"]["steps"]
                      if s.get("id") == "presentation")["run"]
        files = ("libs/appflowy-presentation/Cargo.toml", "tests/presentation_acceptance.rs",
                 "script/test_presentation_acceptance.py", "script/test_presentation_gateway_contract.py")
        for count in (0, 1, 3, 4):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in files[:count]:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                output = root / "output"
                output.touch()
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                                        env={**os.environ, "GITHUB_OUTPUT": str(output)},
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, count in (0, 4), result.stdout + result.stderr)
                if count in (0, 4):
                    self.assertEqual(output.read_text().strip(), f"enabled={str(count == 4).lower()}")

    def test_inventory_retains_database_coverage_and_explicit_special_cases(self):
        self.assertGreaterEqual(sum(len(policy["database"]) for policy in runner.POLICIES.values()), 100)
        self.assertEqual(len(runner.POLICIES["appflowy-cloud-ai"]["media"]), 2)
        for policy in runner.POLICIES.values():
            names = sum((policy[key] for key in ("database", "media", "acceptance", "external")), [])
            self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
