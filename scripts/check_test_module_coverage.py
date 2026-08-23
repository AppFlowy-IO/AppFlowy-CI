#!/usr/bin/env python3
"""Fail when an integration test module exists in AppFlowy-Cloud-Premium but no CI
matrix entry runs it.

The cloud integration workflows select test modules through explicit
``test_modules`` allow-lists (deliberate: sharding and isolation groups are
curated). The failure mode of an allow-list is silence — a new test module that
nobody adds to the matrix simply never runs. This guard turns that silence into
a CI failure that names the missing module.

Coverage rules, mirroring how the workflows invoke ``cargo test "<module>::"``:

* Every top-level module declared in ``tests/main.rs`` ("tree") must be covered.
* A tree is covered when its bare name is listed (runs the whole tree), or —
  for directory trees — when every *test-bearing* submodule of its ``mod.rs``
  is listed as ``tree::submodule`` (or deeper).
* Modules without any ``#[test]``/``#[tokio::test]`` are helpers and exempt.
* Separate test targets (``tests/*.rs`` seed fixtures) are not part of the
  ``main`` target and are exempt by design (they are ``#[ignore]`` suites).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

MOD_RE = re.compile(r"^\s*(?:pub\s+)?mod\s+([A-Za-z0-9_]+)\s*;", re.M)
TEST_ATTR_RE = re.compile(r"#\[\s*(?:tokio\s*::\s*)?test\b")

# Modules that intentionally never run from these matrices. Keep every entry
# commented so an exemption cannot hide an accident.
EXEMPT = {
  # Seeded-fixture verification suites: every test is #[ignore] and targets a
  # developer's persistent fixture data, not the throwaway CI stack.
  "workspace::permission_fixtures",
}


def parse_covered_tokens(workflow_paths: list[pathlib.Path]) -> set[str]:
  """All module tokens inside ``test_modules`` values across the workflows."""
  tokens: set[str] = set()
  token_re = re.compile(r"[A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)*")
  key_re = re.compile(r"^(\s*)(?:-\s*)?test_modules:\s*(.*)$")
  module_line_re = re.compile(r"^\s+[A-Za-z0-9_: ]+$")
  for path in workflow_paths:
    lines = path.read_text().splitlines()
    index = 0
    while index < len(lines):
      key_match = key_re.match(lines[index])
      index += 1
      if not key_match:
        continue
      inline = key_match.group(2).strip().strip("\"'")
      if inline and inline not in {">-", ">", "|-", "|"}:
        tokens.update(t for t in inline.split() if token_re.fullmatch(t))
        continue
      # Folded block: consume the more-indented continuation lines that hold
      # only module tokens (a new `key:` or `- item` line ends the block).
      while index < len(lines) and module_line_re.match(lines[index]):
        tokens.update(
          t for t in lines[index].split() if token_re.fullmatch(t)
        )
        index += 1
  return tokens


def module_sources(base: pathlib.Path, name: str) -> list[pathlib.Path]:
  file = base / f"{name}.rs"
  if file.exists():
    return [file]
  directory = base / name
  if directory.is_dir():
    return sorted(directory.rglob("*.rs"))
  return []


def has_tests(sources: list[pathlib.Path]) -> bool:
  return any(TEST_ATTR_RE.search(src.read_text(errors="ignore")) for src in sources)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--tests", required=True, type=pathlib.Path,
                      help="AppFlowy-Cloud-Premium tests/ directory")
  parser.add_argument("--workflow", required=True, action="append",
                      type=pathlib.Path, help="workflow yaml (repeatable)")
  args = parser.parse_args()

  covered = parse_covered_tokens(args.workflow)
  missing: list[str] = []

  for tree in MOD_RE.findall((args.tests / "main.rs").read_text()):
    sources = module_sources(args.tests, tree)
    if not sources or not has_tests(sources):
      continue  # helper module
    if tree in covered or tree in EXEMPT:
      continue  # the whole tree runs (or is exempt)

    mod_rs = args.tests / tree / "mod.rs"
    if not mod_rs.exists():
      missing.append(tree)
      continue

    for sub in MOD_RE.findall(mod_rs.read_text()):
      qualified = f"{tree}::{sub}"
      if qualified in covered or qualified in EXEMPT:
        continue
      if any(token.startswith(qualified + "::") for token in covered):
        continue
      sub_sources = module_sources(args.tests / tree, sub)
      if sub_sources and has_tests(sub_sources):
        missing.append(qualified)

  if missing:
    print("The following test modules exist but no test_modules entry runs them:")
    for module in sorted(missing):
      print(f"  - {module}")
    print(
      "\nAdd each module to a matrix entry in the cloud integration workflows "
      "(or, for a deliberate exclusion, to EXEMPT in scripts/check_test_module_coverage.py "
      "with a comment saying why)."
    )
    return 1

  print(f"All test-bearing modules are covered ({len(covered)} matrix tokens).")
  return 0


if __name__ == "__main__":
  sys.exit(main())
