"""Regression checks for Cloud integration topic grouping and target coverage."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/cloud_integration_ci.yaml"
SEARCH_INDEX = "database::database_index_test"
SEARCH_RESTORE = "database::database_history_test::search_restore"
SCIM_GROUP_POLICY = "biz::directory::group::tests::scim_group_members_can_exceed_the_manual_group_limit"
SCIM_RETRY_AUDIT = (
  "biz::directory::status::tests::"
  "retry_audits_the_admin_and_rolls_back_when_required_audit_fails"
)


def render_run_tests(lane):
  workflow = yaml.safe_load(WORKFLOW.read_text())
  run = next(step["run"] for step in workflow["jobs"]["test"]["steps"]
             if step.get("name") == "Run Tests")
  replacements = {
    "${{ matrix.test_service }}": lane["test_service"],
    "${{ matrix.test_modules }}": lane.get("test_modules", ""),
    "${{ matrix.test_skips }}": lane.get("test_skips", ""),
    "${{ matrix.test_targets }}": lane.get("test_targets", ""),
    "${{ matrix.test_package_search == true }}": str(
      lane.get("test_package_search", False)
    ).lower(),
    "${{ matrix.test_root_unit == true }}": str(
      lane.get("test_root_unit", False)
    ).lower(),
  }
  for expression, value in replacements.items():
    run = run.replace(expression, value)
  return run


class CloudIntegrationMatrixTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    cls.job = workflow["jobs"]["test"]
    cls.lanes = cls.job["strategy"]["matrix"]["include"]

  def lane(self, service):
    return next(lane for lane in self.lanes if lane["test_service"] == service)

  def test_auth_is_one_serial_topic_with_both_profiles(self):
    auth = self.lane("appflowy_cloud_auth")
    self.assertEqual(auth["topic"], "Auth")
    self.assertEqual(auth["test_modules"].split(), ["scim", "ldap"])
    workflow_text = WORKFLOW.read_text()
    self.assertIn('export COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}authentik"', workflow_text)
    self.assertIn('export COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}ldap"', workflow_text)
    run = render_run_tests(auth)
    self.assertIn('"scim::"', run)
    self.assertIn('"ldap::"', run)

  def test_search_topic_runs_index_restore_and_package(self):
    search = self.lane("appflowy_cloud_search")
    self.assertEqual(search["topic"], "Search")
    self.assertEqual(search["cache_group"], "search")
    self.assertTrue(search["test_package_search"])
    self.assertIn(SEARCH_INDEX, search["test_modules"])
    self.assertIn(SEARCH_RESTORE, search["test_modules"])
    run = render_run_tests(search)
    with tempfile.TemporaryDirectory() as directory:
      calls = Path(directory) / "cargo-calls"
      fake_cargo = (
        'cargo() { printf "%s\\t" "$@" >> "$CARGO_CALLS"; '
        'printf "\\n" >> "$CARGO_CALLS"; }\n' + run
      )
      result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", fake_cargo],
        env={
          **os.environ,
          "CARGO_CALLS": str(calls),
          "CLOUD_RUN_ROOT": "false",
          "RUN_SEARCH_PACKAGE": "true",
          "TEST_SKIPS": "",
        },
        capture_output=True,
        text=True,
        check=False,
      )
      self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
      commands = calls.read_text().splitlines()
      self.assertTrue(any(SEARCH_INDEX in command for command in commands))
      self.assertTrue(any(SEARCH_RESTORE in command for command in commands))
      self.assertTrue(any("-p\tappflowy-search\t" in command for command in commands))

  def run_member_packages(self, *, contracts=("scim", "mcp"), counts=None,
                          ignored=(), execution_exit=0, audit_selected=True,
                          audit_ignored=False, audit_execution_exit=0):
    prefixes = {
      "scim": "api::scim::tests::",
      "status": "biz::directory::status::tests::",
      "mcp": "workspace_token::tests::",
    }
    counts = counts if counts is not None else {"scim": 51, "group": 1, "status": 11, "mcp": 3}
    def case_name(suite, index):
      return SCIM_GROUP_POLICY if suite == "group" else f"{prefixes[suite]}case_{index}"

    with tempfile.TemporaryDirectory() as directory:
      project = Path(directory)
      for contract, relative in {
        "scim": "libs/appflowy-cloud-directory/src/api/scim/user_attributes.rs",
        "mcp": "libs/appflowy-mcp-core/src/workspace_token.rs",
      }.items():
        if contract in contracts:
          source = project / relative
          source.parent.mkdir(parents=True, exist_ok=True)
          source.touch()
      (project / "all-tests").write_text("".join(
        f"{case_name(suite, index)}: test\n"
        for suite, count in counts.items() for index in range(count)
      ))
      (project / "ignored-tests").write_text("".join(
        f"{case_name(suite, 0)}: test\n" for suite in ignored
      ))
      (project / "audit-all-tests").write_text(
        f"{SCIM_RETRY_AUDIT}: test\n" if audit_selected else ""
      )
      (project / "audit-ignored-tests").write_text(
        f"{SCIM_RETRY_AUDIT}: test\n" if audit_ignored else ""
      )
      run = render_run_tests(self.lane("appflowy_cloud_member_packages"))
      fake_cargo = r'''cargo() {
        printf '%s\t' "$@" >> cargo-calls
        printf '\n' >> cargo-calls
        local inventory_prefix= execution_exit="$TEST_EXECUTION_EXIT"
        if [[ " $* " == *" --features appflowy-cloud-directory/self-host-af "* ]]; then
          inventory_prefix=audit-
          execution_exit="$AUDIT_EXECUTION_EXIT"
        fi
        if [[ " $* " == *" --list "* ]]; then
          if [[ " $* " == *" --ignored "* ]]; then
            cat "${inventory_prefix}ignored-tests"
          else
            cat "${inventory_prefix}all-tests"
          fi
        else
          return "$execution_exit"
        fi
      }
      ''' + run
      result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", fake_cargo],
        cwd=project,
        env={
          **os.environ,
          "CLOUD_RUN_ROOT": "false",
          "RUN_ROOT_UNIT_TESTS": "false",
          "RUN_SEARCH_PACKAGE": "false",
          "TEST_TARGETS": "",
          "TEST_SKIPS": "",
          "TEST_EXECUTION_EXIT": str(execution_exit),
          "AUDIT_EXECUTION_EXIT": str(audit_execution_exit),
        },
        capture_output=True,
        text=True,
        check=False,
      )
      calls = (project / "cargo-calls").read_text().splitlines()
      return result, [command.rstrip("\t").split("\t") for command in calls]

  def test_managed_user_guards_and_execution_share_unfiltered_packages(self):
    result, commands = self.run_member_packages()
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(len(commands), 6)
    packages = commands[2][2:-2]
    self.assertEqual(commands[2][-2:], ["--", "--test-threads=1"])
    self.assertEqual(commands[0][2:-3], packages)
    self.assertEqual(commands[1][2:-4], packages)
    self.assertEqual(commands[0][-3:], ["--lib", "--", "--list"])
    self.assertEqual(commands[1][-4:], ["--lib", "--", "--ignored", "--list"])
    self.assertTrue(all(packages[i] == "-p" for i in range(0, len(packages), 2)))
    self.assertIn("appflowy-cloud-directory", packages)
    self.assertIn("appflowy-mcp-core", packages)
    self.assertIn("appflowy-mcp", packages)
    self.assertIn("api::scim::: 51 selected, 0 ignored", result.stdout)
    self.assertIn(f"{SCIM_GROUP_POLICY}: 1 selected, 0 ignored", result.stdout)
    self.assertIn("biz::directory::status::tests::: 11 selected, 0 ignored", result.stdout)
    self.assertIn("3 selected, 0 ignored", result.stdout)
    audit_command = [
      "test", "--locked", "-p", "appflowy-cloud-directory", "--lib",
      "--features", "appflowy-cloud-directory/self-host-af", SCIM_RETRY_AUDIT, "--", "--exact",
    ]
    self.assertEqual(commands[3], audit_command + ["--list"])
    self.assertEqual(commands[4], audit_command + ["--ignored", "--list"])
    self.assertEqual(commands[5], audit_command + ["--test-threads=1"])
    self.assertIn(f"{SCIM_RETRY_AUDIT}: 1 selected, 0 ignored", result.stdout)

  def test_managed_user_guards_allow_old_sources_and_independent_features(self):
    for contracts, counts in [
      ((), {}),
      (("scim",), {"scim": 52, "group": 1, "status": 11}),
      (("mcp",), {"mcp": 4}),
    ]:
      with self.subTest(contracts=contracts):
        result, commands = self.run_member_packages(contracts=contracts, counts=counts)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(commands), 6 if "scim" in contracts else (3 if contracts else 1))
        hosted_execution = commands[2] if contracts else commands[0]
        self.assertIn("appflowy-mcp-core", hosted_execution)
        self.assertEqual(hosted_execution[-2:], ["--", "--test-threads=1"])

  def test_managed_user_guards_reject_missing_or_ignored_cases(self):
    for suite, minimum in {"scim": 51, "group": 1, "status": 11, "mcp": 3}.items():
      for selected, ignored in [(0, ()), (minimum - 1, ()), (minimum, (suite,))]:
        with self.subTest(suite=suite, selected=selected, ignored=ignored):
          counts = {"scim": 51, "group": 1, "status": 11, "mcp": 3, suite: selected}
          result, commands = self.run_member_packages(counts=counts, ignored=ignored)
          self.assertNotEqual(result.returncode, 0)
          self.assertIn("::error::Required SCIM/MCP regression coverage", result.stdout)
          self.assertEqual(len(commands), 2, "Do not continue after a coverage guard fails")

  def test_member_package_test_failures_fail_the_job(self):
    result, commands = self.run_member_packages(execution_exit=42)
    self.assertEqual(result.returncode, 42)
    self.assertEqual(len(commands), 3)

  def test_self_hosted_audit_guard_rejects_missing_or_ignored_case(self):
    for selected, ignored in [(False, False), (True, True)]:
      with self.subTest(selected=selected, ignored=ignored):
        result, commands = self.run_member_packages(
          audit_selected=selected, audit_ignored=ignored
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"coverage is missing or ignored: {SCIM_RETRY_AUDIT}", result.stdout)
        self.assertEqual(len(commands), 5, "Do not execute an empty or ignored audit selection")

  def test_self_hosted_audit_failure_fails_the_job(self):
    result, commands = self.run_member_packages(audit_execution_exit=43)
    self.assertEqual(result.returncode, 43)
    self.assertEqual(len(commands), 6)

  def test_permission_and_space_suites_are_partitioned_without_drops(self):
    lanes = [
      lane for lane in self.lanes
      if lane["test_service"] in {
        "appflowy_cloud_workspace_permissions",
        "appflowy_cloud_workspace_permissions_2",
      }
    ]
    self.assertEqual(
      {lane["topic"] for lane in lanes},
      {"Permissions and spaces", "Permissions and spaces 2"},
    )
    modules = " ".join(lane.get("test_modules", "") for lane in lanes).split()
    self.assertEqual(len(modules), len(set(modules)))
    self.assertIn("workspace::permissions::realtime_enforcement_test", modules)
    self.assertIn("workspace::permissions::share_management", modules)
    self.assertIn("workspace::permissions::page_access", modules)
    self.assertIn("workspace::custom_space_test", modules)

  def test_standalone_target_topic_is_wildcard_and_uses_archive_runner(self):
    standalone = self.lane("appflowy_cloud_standalone_targets")
    self.assertEqual(standalone["test_targets"], "*")
    self.assertIn("cargo metadata --locked --no-deps", render_run_tests(standalone))
    self.assertIn("cloud_test_binaries.py run", render_run_tests(standalone))
    coverage = (REPO / "scripts/check_test_module_coverage.py").read_text()
    self.assertIn('parse_covered_tokens(args.workflow, "test_targets")', coverage)

  def test_search_admission_has_redis_without_application_consumers(self):
    steps = self.job["steps"]
    start = next(step for step in steps if step.get("name") == "Start Search admission test Redis")
    install = next(step for step in steps if step.get("name") == "Install isolated test Redis")
    for step in (start, install):
      self.assertIn("matrix.test_service == 'appflowy_worker'", step["if"])
      self.assertIn("matrix.test_service == 'appflowy_cloud_member_packages'", step["if"])
      self.assertIn("matrix.test_package_search == true", step["if"])
    self.assertIn("--publish 127.0.0.1::6379", start["run"])
    self.assertIn("APPFLOWY_TEST_REDIS_URL=redis://127.0.0.1:$fixture_port", start["run"])
    self.assertIn('>> "$GITHUB_ENV"', start["run"])
    self.assertLess(steps.index(start), next(i for i, step in enumerate(steps)
                                          if step.get("name") == "Run Tests"))
    cleanup = next(step for step in steps if step.get("name") == "Stop Search admission test Redis")
    self.assertEqual(cleanup["if"], "always()")
    self.assertIn('docker rm --force --volumes "$SEARCH_ADMISSION_REDIS_CONTAINER"', cleanup["run"])
    self.assertIn("-p appflowy-mcp", render_run_tests(self.lane("appflowy_cloud_member_packages")))
    self.assertIn("-p server-infra", render_run_tests(self.lane("appflowy_cloud_member_packages")))


if __name__ == "__main__":
  unittest.main()
