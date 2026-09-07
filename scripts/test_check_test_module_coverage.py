#!/usr/bin/env python3
"""Keep SQLx-only integration modules visible to the CI coverage gate."""

import pathlib
import subprocess
import sys
import tempfile
import unittest

import check_test_module_coverage as coverage


class TestModuleCoverage(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = pathlib.Path(temporary.name)
    self.tests = self.root / "tests"
    self.module = self.tests / "sql_test"
    self.module.mkdir(parents=True)
    (self.tests / "main.rs").write_text("mod sql_test;\n")
    (self.module / "mod.rs").write_text("mod sample;\nmod util;\n")
    (self.module / "util.rs").write_text("fn setup_db() {}\n")
    self.sample = self.module / "sample.rs"
    self.sample.write_text("#[sqlx::test(migrations = false)]\nasync fn sample() {}\n")
    self.workflow = self.root / "workflow.yaml"

  def run_checker(self, modules):
    self.workflow.write_text(
      "jobs:\n  tests:\n    strategy:\n      matrix:\n        include:\n"
      f"          - test_modules: '{modules}'\n"
    )
    return subprocess.run(
      [sys.executable, str(pathlib.Path(coverage.__file__).resolve()),
       "--tests", str(self.tests), "--workflow", str(self.workflow)],
      capture_output=True, text=True, check=False,
    )

  def test_recognizes_standard_tokio_and_sqlx_test_attributes(self):
    for attribute in ("#[test]", "#[tokio::test]", "#[sqlx::test]",
                      "#[sqlx::test(migrations = false)]"):
      with self.subTest(attribute=attribute):
        signature = "fn" if attribute == "#[test]" else "async fn"
        self.sample.write_text(f"{attribute}\n{signature} sample() {{}}\n")
        self.assertTrue(coverage.has_tests([self.sample]))

  def test_helpers_do_not_count_as_tests(self):
    self.assertFalse(coverage.has_tests([self.module / "util.rs"]))

  def test_unlisted_sqlx_only_module_fails(self):
    result = self.run_checker("sql_test::other")
    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
    self.assertIn("sql_test::sample", result.stdout)
    self.assertNotIn("sql_test::util", result.stdout)

  def test_explicit_sqlx_module_is_covered(self):
    result = self.run_checker("sql_test::sample")
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

  def test_parent_tree_runs_sqlx_modules(self):
    result = self.run_checker("sql_test")
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

  def test_ignore_attribute_does_not_silently_exempt_a_module(self):
    self.sample.write_text("#[ignore]\n#[sqlx::test]\nasync fn sample() {}\n")
    result = self.run_checker("")
    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
    self.assertIn("sql_test::sample", result.stdout)


if __name__ == "__main__":
  unittest.main()
