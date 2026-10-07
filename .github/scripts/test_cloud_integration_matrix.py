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
