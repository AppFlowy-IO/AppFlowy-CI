"""Exercise member preparation without compiling the Cloud workspace or contacting services."""

import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / ".github/scripts/cloud_member_tests.sh"
WORKFLOW = REPO / ".github/workflows/cloud_integration_ci.yaml"
PACKAGES = [
    "workspace-folder", "collab-storage", "database-blob", "collab-access",
    "collab-indexing", "collab-snapshot", "collab-history", "collab-realtime",
    "collab-realtime-server", "indexer", "collab-stream", "database", "server-infra",
    "workspace-permission", "pdf-exporter", "collab", "collab-rt-entity", "appflowy-proto",
    "appflowy-notification", "appflowy-cloud-core",
    "appflowy-cloud-forms", "appflowy-cloud-workspace-support", "appflowy-cloud-workspace",
    "appflowy-cloud-admin", "appflowy-cloud-collab", "appflowy-cloud-workspace-access",
    "appflowy-cloud-pages", "appflowy-cloud-publishing", "appflowy-cloud-database-api",
    "appflowy-cloud-directory", "appflowy-cloud-history", "appflowy-mcp", "appflowy-mcp-core",
]
GROUP_POLICY = "biz::directory::group::tests::scim_group_members_can_exceed_the_manual_group_limit"


EXPIRED_GROUP = "dispatcher::tests::measure_expired_property_group_dispatch_transaction"


class CloudMemberPreparationTest(unittest.TestCase):
    def invoke(self, mode, *, contracts=True, ignored=False, compile_exit=0, test_exit=0,
               inventory_exit=0, extra_args=(), automation_results=None, expired_group=False,
               compiled_group=True, ignored_group=True, dispatcher_count=12, other_ignored=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if automation_results is not None:
                path = root / "libs/automation-engine/src/dispatcher/checkout_measurement.rs"
                path.parent.mkdir(parents=True)
                path.touch()
                (root / "libs/automation-engine/Cargo.toml").touch()
                (root / "postgres-results").write_text(automation_results)
            if expired_group:
                path = root / "libs/automation-engine/src/dispatcher/tests.rs"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("async fn measure_expired_property_group_dispatch_transaction() {}")
                (root / "libs/automation-engine/Cargo.toml").touch()
            if contracts:
                for relative in ["libs/appflowy-cloud-directory/src/api/scim/user_attributes.rs",
                                 "libs/appflowy-mcp-core/src/workspace_token.rs"]:
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
            inventory = [f"api::scim::tests::case_{index}: test" for index in range(51)]
            inventory += [f"biz::directory::status::tests::case_{index}: test"
                          for index in range(11)]
            inventory += [f"workspace_token::tests::case_{index}: test" for index in range(3)]
            inventory.append(f"{GROUP_POLICY}: test")
            ignored_cases = [inventory[0]] if ignored else []
            if expired_group:
                for prefix, count in [("automation::run_queue_tests::conformance::", 12),
                                      ("dispatcher::tests::", dispatcher_count), ("executor::race_tests::", 10)]:
                    inventory.extend(f"{prefix}case_{n}: test" for n in range(count))
                if compiled_group:
                    inventory.append(f"{EXPIRED_GROUP}: test")
                if ignored_group:
                    ignored_cases.append(f"{EXPIRED_GROUP}: test")
                if other_ignored:
                    ignored_cases.append("dispatcher::tests::case_0: test")
            (root / "all").write_text("\n".join(inventory) + "\n")
            (root / "ignored").write_text("".join(line + "\n" for line in ignored_cases))
            binary = root / "bin"
            binary.mkdir()
            cargo = binary / "cargo"
            cargo.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
with open("calls.jsonl", "a") as output:
    output.write(json.dumps({"args": args, "RUST_LOG": os.getenv("RUST_LOG"),
                            "DISABLE_CI_TEST_LOG": os.getenv("DISABLE_CI_TEST_LOG")}) + "\\n")
if "--ignored" in args and "--exact" in args:
    assert os.environ["NOTIFICATION_TEST_DATABASE_URL"] == os.environ["DATABASE_URL"]
    print(pathlib.Path("postgres-results").read_text(), end="")
if "--list" in args:
    print(pathlib.Path("ignored" if "--ignored" in args else "all").read_text(), end="")
    sys.exit(int(os.environ["INVENTORY_EXIT"]))
if "--no-run" in args:
    sys.exit(int(os.environ["COMPILE_EXIT"]))
sys.exit(int(os.environ["TEST_EXIT"]))
''')
            cargo.chmod(0o755)
            result = subprocess.run(
                ["bash", str(HELPER), mode, *extra_args], cwd=root,
                env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
                     "RUST_LOG": "inherited", "DISABLE_CI_TEST_LOG": "false",
                     "DATABASE_URL": "postgres://member-fixture/postgres",
                     "INVENTORY_EXIT": str(inventory_exit), "COMPILE_EXIT": str(compile_exit),
                     "TEST_EXIT": str(test_exit)},
                text=True, capture_output=True, timeout=10, check=False,
            )
            calls = root / "calls.jsonl"
            return result, ([json.loads(line) for line in calls.read_text().splitlines()]
                            if calls.exists() else [])

    def test_preparation_lists_guards_and_compiles_the_complete_unchanged_graph(self):
        result, calls = self.invoke("prepare")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        package_args = [part for package in PACKAGES for part in ("-p", package)]
        self.assertEqual([call["args"] for call in calls], [
            ["test", "--locked", *package_args, "--lib", "--", "--list"],
            ["test", "--locked", *package_args, "--lib", "--", "--ignored", "--list"],
            ["test", "--locked", *package_args, "--no-run"],
        ])
        self.assertEqual(calls[-1]["RUST_LOG"], "info")
        self.assertEqual(calls[-1]["DISABLE_CI_TEST_LOG"], "true")
        self.assertIn("51 selected, 0 ignored", result.stdout)
        self.assertIn("11 selected, 0 ignored", result.stdout)
        self.assertIn("3 selected, 0 ignored", result.stdout)

    def test_old_sources_still_compile_all_targets_without_contract_guards(self):
        result, calls = self.invoke("prepare", contracts=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"][-1], "--no-run")
        self.assertNotIn("--lib", calls[0]["args"])

    def test_ignored_required_case_stops_before_the_all_target_build(self):
        result, calls = self.invoke("prepare", ignored=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("--list" in call["args"] for call in calls))

    def test_inventory_or_compile_failure_fails_preparation(self):
        for options, expected_calls, exit_code in [
            ({"inventory_exit": 41}, 1, 41), ({"compile_exit": 42}, 3, 42),
        ]:
            with self.subTest(options=options):
                result, calls = self.invoke("prepare", **options)
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assertEqual(len(calls), expected_calls)

    def test_run_preserves_unfiltered_targets_doctests_environment_and_failure(self):
        result, calls = self.invoke("run", test_exit=43)
        self.assertEqual(result.returncode, 43, result.stdout + result.stderr)
        package_args = [part for package in PACKAGES for part in ("-p", package)]
        self.assertEqual(calls, [{
            "args": ["test", "--locked", *package_args, "--", "--test-threads=1"],
            "RUST_LOG": "info", "DISABLE_CI_TEST_LOG": "true",
        }])
        for selector in ("--lib", "--bins", "--tests", "--doc", "--ignore", "--skip"):
            self.assertNotIn(selector, calls[0]["args"])

    def test_automation_requires_both_disposable_postgres_cases_to_execute(self):
        recorder = "a_pool_free_recorder_records_what_the_service_records"
        dispatch = "dispatcher::checkout_measurement::measure_dispatch_checkout_and_connection_hold"
        passed = [f"test {case} ... ok" for case in (recorder, dispatch)]
        for lines, success in [(passed, True), (passed[:1], False),
                               ([f"test {recorder} ... ignored", passed[1]], False),
                               ([passed[0], f"test {dispatch}_other ... ok"], False)]:
            with self.subTest(lines=lines):
                result, calls = self.invoke("run", automation_results="\n".join(lines) + "\n")
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertEqual(len(calls), 2)
                args = calls[1]["args"]
                self.assertEqual(args[:args.index("--lib")], calls[0]["args"][:-2])
                self.assertEqual(args[args.index("--lib"):],
                                 ["--lib", "--test", "delivery", "--", "--exact", "--ignored",
                                  recorder, dispatch, "--test-threads=1", "--show-output"])

    def test_dispatch_guard_requires_regular_cases_and_the_separate_measurement(self):
        for options, success in [({}, True), ({"compiled_group": False}, False),
                                 ({"ignored_group": False}, False), ({"dispatcher_count": 11}, False),
                                 ({"other_ignored": True}, False)]:
            with self.subTest(options=options):
                result, calls = self.invoke("prepare", expired_group=True, **options)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertEqual(len(calls), 3 if success else 2)

    def test_group_measurement_must_have_its_own_passing_result(self):
        other_cases = ("a_pool_free_recorder_records_what_the_service_records",
                       "dispatcher::checkout_measurement::measure_dispatch_checkout_and_connection_hold")
        passed = "".join(f"test {name} ... ok\n" for name in other_cases)
        for tail, success in [(f"test {EXPIRED_GROUP} ... ok\n", True), ("", False),
                              (f"test {EXPIRED_GROUP} ... ignored\n", False),
                              (f"test {EXPIRED_GROUP}_other ... ok\n", False)]:
            with self.subTest(tail=tail):
                result, calls = self.invoke("run", expired_group=True, automation_results=passed + tail)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                args = calls[-1]["args"]
                self.assertEqual(args[args.index("--ignored") + 1:args.index("--test-threads=1")],
                                 [*other_cases, EXPIRED_GROUP])

    def test_invalid_mode_cannot_invoke_cargo(self):
        result, calls = self.invoke("invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_member_preparation_receives_tools_when_root_and_ollama_are_disabled(self):
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]
        steps = job["steps"]
        checkout = next(step for step in steps if step.get("name") == "Checkout CI tools")
        prepare = next(step for step in steps
                       if step.get("name") == "Prepare shared library test binaries")
        member = next(lane for lane in job["strategy"]["matrix"]["include"]
                      if lane["test_service"] == "appflowy_cloud_member_packages")
        # Match the actual member lane: it uses Cargo locally and never selects Ollama.
        self.assertEqual(member["cache_group"], "members")
        self.assertFalse(member.get("test_package_search", False))
        self.assertFalse(member.get("test_package_ai", False))
        self.assertFalse(member.get("test_targets"))
        self.assertFalse(member.get("ollama", False))
        env = {"CLOUD_RUN_ROOT": "false", "AI_ON_OLLAMA": "false"}

        def should_checkout(condition):
            results = []
            for term in condition.split("||"):
                match = re.fullmatch(r"\s*(env|matrix)\.(\w+) == '([^']+)'\s*", term)
                self.assertIsNotNone(match, f"Unsupported checkout condition: {term}")
                scope, key, value = match.groups()
                results.append((env if scope == "env" else member).get(key) == value)
            return any(results)

        self.assertTrue(should_checkout(checkout["if"]))
        self.assertFalse(should_checkout(
            "env.CLOUD_RUN_ROOT == 'true' || env.AI_ON_OLLAMA == 'true'"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["with"]["path"], "ci-tools")
        self.assertLess(steps.index(checkout), steps.index(prepare))
        # Materialize tools only when the real workflow would perform its checkout. This
        # prevents unconditional fixture symlinks from concealing a skipped checkout.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse((root / "ci-tools").exists())
            if should_checkout(checkout["if"]):
                tools = root / checkout["with"]["path"] / ".github/scripts"
                tools.mkdir(parents=True)
                shutil.copy2(HELPER, tools / HELPER.name)
            cargo = root / "cargo"
            cargo.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            cargo.chmod(0o755)
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", prepare["run"]], cwd=root,
                env={**os.environ, **env, "PATH": f"{root}:{os.environ['PATH']}"},
                text=True, capture_output=True, timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("--no-run", result.stdout)

    def test_workflow_prepares_only_members_outside_the_unchanged_test_deadline(self):
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]
        lanes = job["strategy"]["matrix"]["include"]
        member = next(lane for lane in lanes
                      if lane["test_service"] == "appflowy_cloud_member_packages")
        self.assertEqual(member["timeout_minutes"], 105)
        self.assertEqual([lane["test_service"] for lane in lanes
                          if lane.get("timeout_minutes") == 105], [member["test_service"]])
        steps = job["steps"]
        prepare = next(step for step in steps
                       if step.get("name") == "Prepare shared library test binaries")
        run = next(step for step in steps if step.get("name") == "Run Tests")
        self.assertEqual(prepare["if"], "matrix.test_service == 'appflowy_cloud_member_packages'")
        self.assertEqual(prepare["timeout-minutes"], 45)
        self.assertIn("bash ci-tools/.github/scripts/cloud_member_tests.sh prepare", prepare["run"])
        self.assertEqual(run["timeout-minutes"], 50)
        self.assertIn("bash ci-tools/.github/scripts/cloud_member_tests.sh run", run["run"])
        self.assertLess(steps.index(prepare), steps.index(run))
        self.assertEqual(job["timeout-minutes"], "${{ matrix.timeout_minutes || 60 }}")


if __name__ == "__main__":
    unittest.main()
