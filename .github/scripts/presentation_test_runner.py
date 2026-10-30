#!/usr/bin/env python3
"""Run isolated package contracts; every ignored test has an explicit fixture owner."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse


POLICIES = json.loads(Path(__file__).with_name("presentation_test_inventory.json").read_text())
SUMMARY = re.compile(r"test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored;")


@dataclass(frozen=True)
class Binary:
    path: Path
    cwd: Path


def test_names(output):
    names = [line[:-6] for line in output.splitlines() if line.endswith(": test")]
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate names in libtest inventory")
    return set(names)


def require_tests(label, selected, minimum, *, required=()):
    """Require real discovery before execution; required names cannot disappear on rename."""
    missing = set(required) - selected
    print(f"{label}: {len(selected)} selected (minimum {minimum})", flush=True)
    if len(selected) < minimum or missing:
        raise RuntimeError(f"Required tests missing: {label}; missing={sorted(missing)}")


def require_passed(output, expected):
    summaries = SUMMARY.findall(output)
    if not summaries or any(int(failed) or int(ignored) for _, failed, ignored in summaries):
        raise RuntimeError("Missing successful libtest summary or unexpectedly ignored tests")
    if sum(int(passed) for passed, _, _ in summaries) != expected:
        raise RuntimeError(f"Expected {expected} executed tests, got {summaries}")


def execute(command, run, *, cwd, environment, timeout):
    result = run(command, cwd=cwd, environment=environment, capture_output=True,
                 inherit_stderr=True, check=False, timeout=timeout)
    print(result.stdout, flush=True)
    result.check_returncode()
    return result


def build_artifacts(source, package, profile, run, *, manifest=None, selection=()):
    """Use Cargo-emitted artifacts, package identity and cwd, as the existing binary runners do.

    One package per invocation retains its default feature graph. In particular, adding these
    owners to the shared member-package command would unify unrelated dependency features.
    """
    source = source.resolve()
    manifest = (manifest or source / "Cargo.toml").resolve()
    metadata = json.loads(run(["cargo", "metadata", "--locked", "--no-deps",
                               "--format-version=1", "--manifest-path", str(manifest)],
                              cwd=source, capture_output=True, check=True, timeout=120).stdout)
    owner = next(p for p in metadata["packages"] if p["name"] == package)
    owner_manifest = Path(owner["manifest_path"]).resolve()
    result = run(["cargo", "test", "--locked", "--profile", profile,
                  "--manifest-path", str(manifest), "-p", package, *selection,
                  "--no-run", "--message-format=json"],
                 cwd=source, capture_output=True, inherit_stderr=True, check=False, timeout=7200)
    artifacts = []
    for line in result.stdout.splitlines():
        item = json.loads(line)
        if item.get("reason") == "compiler-message":
            print(item["message"].get("rendered", ""), end="", file=sys.stderr)
        if (item.get("reason") != "compiler-artifact" or item.get("package_id") != owner["id"]
                or not item.get("profile", {}).get("test") or not item.get("executable")):
            continue
        if (Path(item["manifest_path"]).resolve() != owner_manifest
                or "self-host-af" in item.get("features", [])):
            raise RuntimeError(f"Unexpected manifest or self-host feature for {package}")
        path = Path(item["executable"]).resolve()
        if not path.is_file() or not path.is_relative_to(Path(metadata["target_directory"]).resolve()):
            raise RuntimeError(f"Invalid Cargo test executable for {package}")
        artifacts.append(Binary(path, owner_manifest.parent))
    result.check_returncode()
    if not artifacts or len({a.path for a in artifacts}) != len(artifacts):
        raise RuntimeError(f"Missing or duplicated Cargo test artifacts for {package}")
    return artifacts


def check_database(environment, run):
    # Never infer a developer's database. The workflow supplies a dedicated, empty service.
    dsn = environment.get("DATABASE_URL", "")
    parsed = urlparse(dsn)
    if (environment.get("APPFLOWY_PRESENTATION_CI_OWNED_POSTGRES") != "1"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.path != "/presentation_ci"
            or any(environment.get(key) != dsn for key in
                   ("APPFLOWY_PRESENTATION_TEST_DATABASE_URL", "TEST_DATABASE_URL"))):
        raise RuntimeError("Requires the owned loopback presentation_ci PostgreSQL service and explicit test DSNs")
    result = run(["psql", "--no-psqlrc", dsn, "-Atc",
                  "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user"],
                 capture_output=True, check=True, timeout=30)
    if result.stdout.strip() != "t":
        raise RuntimeError("Owned PostgreSQL role must be able to create SQLx databases")


def configure_media(environment, run):
    for name, variable, flag in (
        ("ffmpeg", "APPFLOWY_PRESENTATION_TEST_FFMPEG", "-version"),
        ("ffprobe", "APPFLOWY_PRESENTATION_TEST_FFPROBE", "-version"),
        ("pdftoppm", "APPFLOWY_PRESENTATION_TEST_PDFTOPPM", "-v"),
    ):
        path = shutil.which(name)
        if not path:
            raise RuntimeError(f"Presentation media prerequisite missing: {name}")
        run([path, flag], capture_output=True, check=True, timeout=10)
        environment[variable] = path


def run_contracts(package, binaries, run, environment):
    policy = POLICIES[package]
    inventories = []
    ignored_owners = {}
    ordinary_names = set()
    for binary in binaries:
        def listing(*args):
            return test_names(run([str(binary.path), *args, "--list"], cwd=binary.cwd,
                                  environment=environment, capture_output=True,
                                  check=True, timeout=30).stdout)
        all_tests, ignored = listing(), listing("--ignored")
        if not ignored.issubset(all_tests):
            raise RuntimeError("Ignored inventory contains undiscovered tests")
        ordinary = all_tests - ignored
        ordinary_names.update((str(binary.path), name) for name in ordinary)
        for name in ignored:
            if name in ignored_owners:
                raise RuntimeError(f"Ambiguous ignored test: {name}")
            ignored_owners[name] = binary
        inventories.append((binary, ordinary, ignored))
    require_tests(f"{package}/ordinary", ordinary_names, policy["ordinary_minimum"])
    expected_ignored = set().union(*(set(policy[key]) for key in
                                    ("database", "media", "acceptance", "external")))
    require_tests(f"{package}/ignored", set(ignored_owners), len(expected_ignored), required=expected_ignored)
    unexpected = set(ignored_owners) - expected_ignored
    if unexpected:
        raise RuntimeError(f"Unclassified ignored tests need a reviewed fixture owner: {sorted(unexpected)}")
    for binary, ordinary, ignored in inventories:
        if not ordinary:
            raise RuntimeError(f"Test target contains no ordinary tests: {binary.path.name}")
        skips = [arg for name in sorted(ignored) for arg in ("--skip", name)]
        selected = test_names(run([str(binary.path), *skips, "--list"], cwd=binary.cwd,
                                  environment=environment, capture_output=True,
                                  check=True, timeout=30).stdout)
        if selected != ordinary:
            raise RuntimeError("Ignore exclusions changed ordinary test selection")
        result = execute([str(binary.path), *skips, "--test-threads=1"], run, cwd=binary.cwd,
                         environment=environment, timeout=1800)
        require_passed(result.stdout, len(ordinary))
    for name in policy["database"] + policy["media"]:
        binary = ignored_owners[name]
        command = [str(binary.path), name, "--exact", "--ignored"]
        selected = test_names(run(command + ["--list"], cwd=binary.cwd,
                                  environment=environment, capture_output=True,
                                  check=True, timeout=30).stdout)
        require_tests(name, selected, 1, required=[name])
        if selected != {name}:
            raise RuntimeError("Exact ignored selection is ambiguous")
        result = execute(command + ["--test-threads=1"], run, cwd=binary.cwd,
                         environment=environment, timeout=300)
        require_passed(result.stdout, 1)
    for name in policy["acceptance"]:
        print(f"OWNED BY REQUIRED Presentation acceptance lane: {name}")
    for name in policy["external"]:
        print(f"NOT RUN (requires paid image provider and explicit opt-in): {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--package", choices=POLICIES, required=True)
    parser.add_argument("--profile", default="ci")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve() / "script"))
    # Reuse the source's bounded process-group runner for Cargo, libtest and child processes.
    from durable_recovery_runner import run_owned
    environment = dict(os.environ, SQLX_OFFLINE="true")
    check_database(environment, run_owned)
    if POLICIES[args.package]["media"]:
        configure_media(environment, run_owned)
    selection = ("--bin", "appflowy_ai") if args.package == "appflowy-ai" else ()
    binaries = build_artifacts(args.source, args.package, args.profile, run_owned, selection=selection)
    run_contracts(args.package, binaries, run_owned, environment)
    if args.package != "appflowy-ai":
        run_owned(["cargo", "test", "--locked", "--profile", args.profile, "-p", args.package,
                   "--doc"], cwd=args.source.resolve(), check=True, timeout=1800)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"::error::Presentation contracts: {error}", file=sys.stderr)
        sys.exit(1)
