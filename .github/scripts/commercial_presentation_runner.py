#!/usr/bin/env python3
"""Require presentation contracts against the commercial lane's running self-host images."""

import argparse
import os
from pathlib import Path
import subprocess

from presentation_test_runner import require_passed, require_tests, test_names

MODULE = "commercial_test::presentation_test::"
REQUIRED = {
    MODULE + "commercial_test_self_hosted_presentation_ai_license_and_generation",
    MODULE + "commercial_test_self_hosted_presentation_status_and_admin_routes",
    MODULE + "commercial_test_self_hosted_presentation_map_styles_are_unmetered",
    MODULE + "commercial_test_self_hosted_presentation_exports_and_templates_are_unmetered",
}


def run_contracts(source, run=subprocess.run, profile="dev"):
    source = source.resolve()
    tests = source / "tests/commercial_test/presentation_test.rs"
    marker = source / "libs/appflowy-cloud-ai/src/biz/presentation/source/resolver/read_policy.rs"
    if not marker.is_file() and not tests.is_file():
        print("Older source predates commercial presentation contracts", flush=True)
        return
    if not tests.is_file():
        raise RuntimeError("Presentation follow-up source is missing its commercial contracts")
    command = ["cargo", "test", "--locked", "--profile", profile, "--test", "main",
               "--features=commercial-ci", MODULE, "--"]
    environment = dict(os.environ)
    if environment.get("APPFLOWY_PRESENTATION_COMMERCIAL_OWNED_FIXTURE") != "1":
        raise RuntimeError("Requires an explicitly owned commercial fixture (changes its test license)")

    def invoke(arguments):
        result = run(command + arguments, cwd=source, text=True, env=environment,
                     stdout=subprocess.PIPE, check=True, timeout=7200)
        print(result.stdout, flush=True)
        return result.stdout

    selected = test_names(invoke(["--list"]))
    require_tests("real self-host presentation", selected, len(REQUIRED), required=REQUIRED)
    if test_names(invoke(["--ignored", "--list"])):
        raise RuntimeError("Commercial presentation contracts must not be ignored")
    require_passed(invoke(["--test-threads=1", "--nocapture"]), len(selected))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--profile", default="dev")
    args = parser.parse_args()
    run_contracts(args.source, profile=args.profile)
