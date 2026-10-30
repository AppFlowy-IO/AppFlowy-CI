#!/usr/bin/env python3
"""Own the native/full-stack and real Gateway presentation acceptance prerequisites."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

from presentation_test_runner import (
    POLICIES, build_artifacts, check_database, execute, require_passed, require_tests, test_names,
)


ROOT_TESTS = (
    "production_sources_reauthorize_and_private_objects_use_real_s3",
    "web_research::web_search_toggle_preparation_cache_and_failure_contract",
)
NATIVE_TEST = next(name for name in POLICIES["appflowy-cloud-ai"]["acceptance"]
                   if "authorization_tests" in name)
CLOUD_BRIDGE = next(name for name in POLICIES["appflowy-cloud-ai"]["acceptance"]
                    if "gateway_contract" in name)
NATIVE_TESTS = tuple(name for name in POLICIES["appflowy-cloud-ai"]["acceptance"] if name != CLOUD_BRIDGE)
GATEWAY_BRIDGE = "api::ai_usage_tests::bridge::presentation_gateway_contract_bridge"


def prerequisites(source, service, revision):
    if not re.fullmatch(r"[a-f0-9]{40}", revision or ""):
        raise RuntimeError("Set PRESENTATION_SERVICE_SHA to a reviewed immutable Service commit; acceptance cannot skip")
    paths = (
        source / "script/test_presentation_acceptance.py",
        source / "script/test_presentation_gateway_contract.py",
        service / "appflowy-cloud-gateway/Cargo.toml",
        service / "appflowy-cloud-billing/migrations",
        service / "appflowy-cloud-gateway/migrations",
    )
    if any(not path.exists() for path in paths):
        raise RuntimeError("Acceptance requires the matching Service Gateway test target and Billing/Gateway migrations")
    actual = subprocess.check_output(["git", "-C", str(service), "rev-parse", "HEAD"], text=True).strip()
    if actual != revision:
        raise RuntimeError("Presentation Service checkout does not match PRESENTATION_SERVICE_SHA")


def exact_ignored(binary, names, run, environment, *, complete=False):
    ignored = test_names(run([str(binary.path), "--ignored", "--list"], cwd=binary.cwd,
                             environment=environment, capture_output=True, check=True, timeout=30).stdout)
    require_tests(binary.path.name, ignored, len(names), required=names)
    if complete:
        all_tests = test_names(run([str(binary.path), "--list"], cwd=binary.cwd,
                                   environment=environment, capture_output=True,
                                   check=True, timeout=30).stdout)
        if ignored != set(names) or all_tests != set(names):
            raise RuntimeError("Unclassified root presentation acceptance tests require an owning runner selection")


def run_acceptance(source, service, profile, run):
    environment = dict(os.environ, SQLX_OFFLINE="true")
    check_database(environment, run)
    # Compile each owning package independently and use only this invocation's Cargo artifacts.
    # The root target must stay out of the generic wildcard lane: both of its tests are ignored.
    root = build_artifacts(source, "appflowy-cloud", profile, run,
                           selection=("--test", "presentation_acceptance"))
    if len(root) != 1:
        raise RuntimeError("Expected one presentation acceptance target")
    exact_ignored(root[0], ROOT_TESTS, run, environment, complete=True)
    cloud = build_artifacts(source, "appflowy-cloud-ai", profile, run, selection=("--lib",))
    gateway = build_artifacts(service, "appflowy-cloud-gateway", profile, run,
                              manifest=service / "appflowy-cloud-gateway/Cargo.toml",
                              selection=("--lib",))
    if len(cloud) != 1 or len(gateway) != 1:
        raise RuntimeError("Expected one Cloud AI and one Gateway library test executable")
    exact_ignored(cloud[0], [*NATIVE_TESTS, CLOUD_BRIDGE], run, environment)
    exact_ignored(gateway[0], [GATEWAY_BRIDGE], run, environment)
    # The source-owned runner builds the actual hosted Cloud binary and provisions its own
    # PostgreSQL, GoTrue, Redis and private MinIO with random ports and history-aware migrations.
    result = execute([sys.executable, "-B", str(source / "script/test_presentation_acceptance.py"),
                  "--profile", profile, "--service-root", str(service)], cwd=source,
                 run=run, environment=environment, timeout=7200)
    require_passed(result.stdout, 3)  # native permission + root ACL/S3 + root web research
    # Additional native SQLx cases own empty databases on this lane's explicit PostgreSQL 16
    # service and apply Cloud history themselves. They never run on the ordinary package fixture.
    for name in NATIVE_TESTS:
        if name == NATIVE_TEST:  # already executed by the source-owned full-stack runner
            continue
        result = execute([str(cloud[0].path), name, "--exact", "--ignored", "--test-threads=1"],
                         run=run, cwd=cloud[0].cwd, environment=environment, timeout=300)
        require_passed(result.stdout, 1)
    result = execute([sys.executable, "-B", str(source / "script/test_presentation_gateway_contract.py"),
                  "--cloud-test-binary", str(cloud[0].path),
                  "--gateway-test-binary", str(gateway[0].path)], cwd=source,
                 run=run, environment=environment, timeout=600)
    require_passed(result.stdout, 1)  # Gateway harness also must exit successfully inside the runner
    if "API02 actual Gateway router -> HostedPresentationUsage -> Cloud HTTP -> AppResponse: PASS" not in result.stdout:
        raise RuntimeError("The actual Gateway bridge did not complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--service-source", type=Path, required=True)
    parser.add_argument("--profile", default="ci")
    args = parser.parse_args()
    source, service = args.source.resolve(), args.service_source.resolve()
    prerequisites(source, service, os.environ.get("PRESENTATION_SERVICE_SHA"))
    sys.path.insert(0, str(source / "script"))
    from durable_recovery_runner import run_owned
    run_acceptance(source, service, args.profile, run_owned)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"::error::Presentation acceptance prerequisite/contract: {error}", file=sys.stderr)
        sys.exit(1)
