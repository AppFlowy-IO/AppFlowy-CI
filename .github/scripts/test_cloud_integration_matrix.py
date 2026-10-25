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
SELF_HOST_STATUS_AUDIT = "biz::directory::status::tests::retry_audits_the_admin_and_rolls_back_when_required_audit_fails"


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

  def test_sql_job_budget_includes_artifact_transfer_without_relaxing_test_deadline(self):
    self.assertEqual(self.job["timeout-minutes"], "${{ matrix.timeout_minutes || 60 }}")
    self.assertEqual(self.lane("appflowy_cloud_sql")["timeout_minutes"], 90)
    run_tests = next(step for step in self.job["steps"] if step.get("name") == "Run Tests")
    self.assertEqual(run_tests["timeout-minutes"], 50)

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
                          ignored=(), execution_exit=0, self_host_count=12,
                          self_host_audit=True, self_host_ignored=False,
                          self_host_execution_exit=0,
                          service="appflowy_cloud_member_packages"):
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
      self_host_names = [
        f"{prefixes['status']}case_{index}"
        for index in range(self_host_count - int(self_host_audit))
      ]
      if self_host_audit:
        self_host_names.append(SELF_HOST_STATUS_AUDIT)
      (project / "self-host-tests").write_text("".join(
        f"{name}: test\n" for name in self_host_names
      ))
      (project / "self-host-ignored-tests").write_text(
        f"{SELF_HOST_STATUS_AUDIT}: test\n" if self_host_ignored else ""
      )
      run = render_run_tests(self.lane(service))
      fake_cargo = r'''cargo() {
        printf '%s\t' "$@" >> cargo-calls
        printf '\n' >> cargo-calls
        if [[ " $* " == *" --features self-host-af "* ]]; then
          if [[ " $* " == *" --list "* ]]; then
            if [[ " $* " == *" --ignored "* ]]; then
              cat self-host-ignored-tests
            else
              cat self-host-tests
            fi
          else
            return "$SELF_HOST_EXECUTION_EXIT"
          fi
        elif [[ " $* " == *" --list "* ]]; then
          if [[ " $* " == *" --ignored "* ]]; then
            cat ignored-tests
          else
            cat all-tests
          fi
        else
          return "$TEST_EXECUTION_EXIT"
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
          "SELF_HOST_EXECUTION_EXIT": str(self_host_execution_exit),
        },
        capture_output=True,
        text=True,
        check=False,
      )
      calls_path = project / "cargo-calls"
      calls = calls_path.read_text().splitlines() if calls_path.exists() else []
      return result, [command.rstrip("\t").split("\t") for command in calls]

  def test_managed_user_guards_and_execution_share_unfiltered_packages(self):
    result, commands = self.run_member_packages()
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(len(commands), 3)
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
    self.assertIn("11 selected, 0 ignored", result.stdout)
    self.assertIn("3 selected, 0 ignored", result.stdout)
    self.assertFalse(any("self-host-af" in command for command in commands))

  def test_self_host_status_runs_once_in_a_separate_feature_graph(self):
    result, commands = self.run_member_packages(service="appflowy_cloud_directory_self_host")
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(len(commands), 3)
    self.assertIn("12 selected, 0 ignored", result.stdout)
    self.assertIn(f"{SELF_HOST_STATUS_AUDIT}: 1 selected, 0 ignored", result.stdout)
    self_host = ["test", "--locked", "-p", "appflowy-cloud-directory",
                 "--features", "self-host-af", "--lib", "biz::directory::status::tests::"]
    self.assertEqual(commands[0], self_host + ["--", "--list"])
    self.assertEqual(commands[1], self_host + ["--", "--ignored", "--list"])
    self.assertEqual(commands[2], self_host + ["--", "--test-threads=1"])

  def test_managed_user_guards_allow_old_sources_and_independent_features(self):
    for contracts, counts in [
      ((), {}),
      (("scim",), {"scim": 52, "group": 1, "status": 11}),
      (("mcp",), {"mcp": 4}),
    ]:
      with self.subTest(contracts=contracts):
        result, commands = self.run_member_packages(contracts=contracts, counts=counts)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(commands), 3 if contracts else 1)
        default_execution = commands[2] if contracts else commands[0]
        self.assertIn("appflowy-mcp-core", default_execution)
        self.assertEqual(default_execution[-2:], ["--", "--test-threads=1"])
        self.assertFalse(any("self-host-af" in command for command in commands))
        self_host_result, self_host_commands = self.run_member_packages(
          contracts=contracts, counts=counts, service="appflowy_cloud_directory_self_host")
        self.assertEqual(self_host_result.returncode, 0, self_host_result.stdout + self_host_result.stderr)
        self.assertEqual(len(self_host_commands), 3 if "scim" in contracts else 0)

  def test_managed_user_guards_reject_missing_or_ignored_cases(self):
    for suite, minimum in {"scim": 51, "group": 1, "status": 11, "mcp": 3}.items():
      for selected, ignored in [(0, ()), (minimum - 1, ()), (minimum, (suite,))]:
        with self.subTest(suite=suite, selected=selected, ignored=ignored):
          counts = {"scim": 51, "group": 1, "status": 11, "mcp": 3, suite: selected}
          result, commands = self.run_member_packages(counts=counts, ignored=ignored)
          self.assertNotEqual(result.returncode, 0)
          self.assertIn("::error::Required SCIM/MCP regression coverage", result.stdout)
          self.assertEqual(len(commands), 2, "Do not continue after a coverage guard fails")

  def test_self_host_status_guard_requires_the_real_audit_case_and_no_ignored_cases(self):
    for options in [
      {"self_host_count": 11},
      {"self_host_audit": False},
      {"self_host_ignored": True},
    ]:
      with self.subTest(options=options):
        result, commands = self.run_member_packages(service="appflowy_cloud_directory_self_host", **options)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::Required SCIM/MCP regression coverage", result.stdout)
        self.assertEqual(len(commands), 2, "Do not execute the self-host suite with incomplete coverage")

  def test_self_host_status_execution_failure_fails_the_job(self):
    result, commands = self.run_member_packages(
      service="appflowy_cloud_directory_self_host", self_host_execution_exit=43)
    self.assertEqual(result.returncode, 43)
    self.assertEqual(len(commands), 3)

  def test_member_package_test_failures_fail_the_job(self):
    result, commands = self.run_member_packages(execution_exit=42)
    self.assertEqual(result.returncode, 42)
    self.assertEqual(len(commands), 3)

  def test_directory_feature_lanes_preserve_source_fixture_and_time_budgets(self):
    hosted = self.lane("appflowy_cloud_member_packages")
    self_host = self.lane("appflowy_cloud_directory_self_host")
    self.assertEqual(self_host["topic"], "Directory self-host contracts")
    self.assertEqual(self_host["cache_group"], "directory-self-host")
    self.assertNotEqual(hosted["cache_group"], self_host["cache_group"])
    self.assertTrue(hosted["cache_writer"])
    self.assertTrue(self_host["cache_writer"])
    self.assertEqual(self_host["test_modules"], "")
    self.assertEqual(self.job["runs-on"], "ubuntu-24.04")
    self.assertEqual(self.job["timeout-minutes"], "${{ matrix.timeout_minutes || 60 }}")
    self.assertNotIn("timeout_minutes", self.lane("appflowy_cloud_directory_self_host"))
    self.assertEqual(self.job["strategy"]["max-parallel"], 15)
    steps = self.job["steps"]
    self.assertEqual(next(step for step in steps if step.get("name") == "Run Tests")["timeout-minutes"], 50)
    self.assertEqual(steps[0]["with"]["ref"], "${{ needs.image_source.outputs.sha }}")
    self.assertIn("build_self_hosted", self.job["needs"])
    self.assertEqual(len([lane for lane in self.lanes
                         if lane.get("cache_group") == "directory-self-host"]), 1)

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

  def test_ai_lane_runs_root_modules_then_the_package_built_from_source(self):
    ai = self.lane("appflowy_cloud_api")
    self.assertEqual(ai["topic"], "AI and authentication")
    self.assertEqual(ai["cache_group"], "ai")
    self.assertTrue(ai["test_package_ai"])
    self.assertIn("ai_test", ai["test_modules"].split())
    self.assertFalse(any(lane["test_service"] == "appflowy_cloud_ai_service" for lane in self.lanes))
    run = render_run_tests(ai)

    def execute(extra_env):
      with tempfile.TemporaryDirectory() as directory:
        calls = Path(directory) / "cargo-calls"
        docker_calls = Path(directory) / "docker-calls"
        package_secret = Path(directory) / "package-secret"
        fake_cargo = r'''
          cargo() {
            printf '%s\t' "$@" >> "$CARGO_CALLS"
            printf '\n' >> "$CARGO_CALLS"
            if [[ " $* " == *" -p appflowy-ai "* ]]; then
              printf '%s' "${APPFLOWY_GOTRUE_JWT_SECRET:-}" > "$PACKAGE_SECRET"
              return "${MOCK_AI_EXIT:-0}"
            fi
          }
          docker() {
            printf '%s\t' "$@" >> "$DOCKER_CALLS"
            printf '\n' >> "$DOCKER_CALLS"
            printf '%s' "$MOCK_AI_JWT_SECRET"
            return "${MOCK_DOCKER_EXIT:-0}"
          }
        ''' + run
        result = subprocess.run(
          ["bash", "-e", "-o", "pipefail", "-c", fake_cargo],
          env={
            **os.environ,
            "CARGO_CALLS": str(calls),
            "DOCKER_CALLS": str(docker_calls),
            "PACKAGE_SECRET": str(package_secret),
            "MOCK_AI_JWT_SECRET": "ai-container-test-secret",
            "APPFLOWY_GOTRUE_JWT_SECRET": "stale-host-test-secret",
            "CLOUD_RUN_ROOT": "false",
            "RUN_SEARCH_PACKAGE": "false",
            "RUN_AI_PACKAGE": "true",
            "TEST_SKIPS": "",
            "AI_EVALUATOR_OPENAI_API_KEY": "",
            **extra_env,
          },
          capture_output=True,
          text=True,
          check=False,
        )
        commands = calls.read_text().splitlines() if calls.exists() else []
        container_commands = docker_calls.read_text().splitlines() if docker_calls.exists() else []
        secret = package_secret.read_text() if package_secret.exists() else None
        self.assertNotIn("ai-container-test-secret", result.stdout + result.stderr)
        return result, commands, container_commands, secret

    modules = ai["test_modules"].split()
    # No AI archive from the Cloud image build: the root modules run, the package does not.
    result, commands, container_commands, secret = execute({"AI_IMAGE_FROM_SOURCE": "false"})
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(len(commands), len(modules))
    self.assertFalse(any("-p\tappflowy-ai\t" in command for command in commands))
    self.assertEqual(container_commands, [])
    self.assertIsNone(secret)
    self.assertIn("was not built from this Cloud source", result.stdout)
    # Partial provider credentials: OpenAI and Anthropic models only, Gemini cases skipped.
    result, commands, container_commands, secret = execute({"AI_IMAGE_FROM_SOURCE": "true", "AI_TEST_HAS_FULL_PROVIDERS": "false"})
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(len(commands), len(modules) + 1)
    package = commands[-1]
    self.assertIn("-p\tappflowy-ai\t", package)
    self.assertIn("--skip\tcommercial_test\t", package)
    self.assertIn("--skip\tcompletion_test::no_context_test\t", package)
    self.assertTrue(package.endswith("--test-threads=1\t"))
    self.assertIn("::warning::", result.stdout)
    self.assertEqual(container_commands, [
      "compose\t-f\tdocker-compose-ci.yml\texec\t-T\tai\tprintenv\tAPPFLOWY_GOTRUE_JWT_SECRET\t"
    ])
    self.assertEqual(secret, "ai-container-test-secret")
    # Every provider configured: the whole package runs after the root modules.
    result, commands, _, secret = execute({"AI_IMAGE_FROM_SOURCE": "true", "AI_TEST_HAS_FULL_PROVIDERS": "true"})
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(len(commands), len(modules) + 1)
    self.assertNotIn("completion_test::no_context_test", commands[-1])
    self.assertNotIn("::warning::", result.stdout)
    self.assertEqual(secret, "ai-container-test-secret")
    # A missing/blank secret or failed container read must stop before package execution.
    for extra in ({"MOCK_AI_JWT_SECRET": ""}, {"MOCK_AI_JWT_SECRET": " \t\n"},
                  {"MOCK_DOCKER_EXIT": "1"}):
      with self.subTest(extra=extra):
        result, commands, _, secret = execute({"AI_IMAGE_FROM_SOURCE": "true", **extra})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(commands), len(modules))
        self.assertIsNone(secret)
        self.assertIn("AI container JWT secret", result.stdout + result.stderr)
    result, commands, _, secret = execute({"AI_IMAGE_FROM_SOURCE": "true", "MOCK_AI_EXIT": "101"})
    self.assertEqual(result.returncode, 101)
    self.assertEqual(len(commands), len(modules) + 1)
    self.assertEqual(secret, "ai-container-test-secret")

  def test_search_admission_has_redis_without_application_consumers(self):
    steps = self.job["steps"]
    start = next(step for step in steps if step.get("name") == "Start Search admission test Redis")
    install = next(step for step in steps if step.get("name") == "Install isolated test Redis")
    for step in (start, install):
      self.assertIn("matrix.test_service == 'appflowy_worker'", step["if"])
      self.assertIn("matrix.test_service == 'appflowy_cloud_member_packages'", step["if"])
      self.assertIn("matrix.test_service == 'appflowy_cloud_directory_self_host'", step["if"])
      self.assertIn("matrix.test_package_search == true", step["if"])
    self.assertIn("--publish 127.0.0.1::6379", start["run"])
    self.assertIn("--maxmemory 512mb --maxmemory-policy noeviction", start["run"])
    self.assertIn("APPFLOWY_TEST_REDIS_URL=redis://127.0.0.1:$fixture_port", start["run"])
    self.assertIn('>> "$GITHUB_ENV"', start["run"])
    self.assertLess(steps.index(start), next(i for i, step in enumerate(steps)
                                          if step.get("name") == "Run Tests"))
    cleanup = next(step for step in steps if step.get("name") == "Stop Search admission test Redis")
    self.assertEqual(cleanup["if"], "always()")
    self.assertIn('docker rm --force --volumes "$SEARCH_ADMISSION_REDIS_CONTAINER"', cleanup["run"])
    self.assertIn("-p appflowy-mcp", render_run_tests(self.lane("appflowy_cloud_member_packages")))
    self.assertIn("-p server-infra", render_run_tests(self.lane("appflowy_cloud_member_packages")))

  def test_only_search_serves_the_fake_vision_model(self):
    fake_vision_lanes = [lane["test_service"] for lane in self.lanes if lane.get("fake_vision")]
    self.assertEqual(fake_vision_lanes, ["appflowy_cloud_search"])
    self.assertEqual(
      self.job["env"]["APPFLOWY_AI_PROVIDER_REFRESH_INTERVAL_SECS"],
      "${{ matrix.fake_vision == true && '2' || '15' }}",
    )
    steps = self.job["steps"]
    compose = next(step for step in steps if step.get("name") == "Run Docker-Compose")
    self.assertIn('export COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}fake-ai"',
                  compose["run"])
    wait = next(step for step in steps if step.get("name") == "Wait for the fake AI provider")
    self.assertEqual(wait["if"], "matrix.fake_vision == true")
    self.assertIn("curl -fsS http://localhost:4010/health", wait["run"])
    with tempfile.TemporaryDirectory() as directory:
      # Older Cloud sources have no fake provider; the wait must not fail their Search job.
      Path(directory, "docker-compose-ci.yml").write_text("services:\n  appflowy_search:\n")
      result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", wait["run"]],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
      )
      self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
      self.assertIn("skipping", result.stdout)
    self.assertLess(steps.index(compose), steps.index(wait))
    run_tests = next(step for step in steps if step.get("name") == "Run Tests")
    self.assertLess(steps.index(wait), steps.index(run_tests))
    self.assertEqual(run_tests["env"]["APPFLOWY_FAKE_AI_IMAGE_SEARCH_TEST"],
                     "${{ matrix.fake_vision == true }}")


if __name__ == "__main__":
  unittest.main()
