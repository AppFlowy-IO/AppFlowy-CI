#!/usr/bin/env python3
"""Require existing-account SCIM recovery against the commercial job's owned stack."""

import argparse
from pathlib import Path
import subprocess

from presentation_test_runner import require_passed, require_tests, test_names

MODULE = "commercial_test::scim_test::identity_recovery::"
REQUIRED = {
    MODULE + "commercial_scim_adopts_auth_only_identity_before_group_access",
    MODULE + "commercial_scim_legacy_case_mirror_resumes_interrupted_user",
    MODULE + "commercial_scim_inactive_auth_only_adoption_preserves_admin_ban_on_group_retry",
}


def run_contracts(source, run=subprocess.run):
    source = source.resolve()
    tests = source / "tests/commercial_test/scim_test/identity_recovery.rs"
    marker = source / "libs/appflowy-cloud-workspace-support/src/biz/user/user_identity/guest.rs"
    if not marker.is_file() and not tests.is_file():
        print("Older source predates SCIM identity recovery contracts", flush=True)
        return
    if not tests.is_file():
        raise RuntimeError("Guest identity recovery is missing its SCIM contracts")
    command = ["cargo", "test", "--locked", "--test", "main",
               "--features=commercial-ci", MODULE, "--"]

    def invoke(arguments):
        result = run(command + arguments, cwd=source, text=True,
                     stdout=subprocess.PIPE, check=False, timeout=7200)
        print(result.stdout, flush=True)
        result.check_returncode()
        return result.stdout

    selected = test_names(invoke(["--list"]))
    require_tests("SCIM identity recovery", selected, len(REQUIRED), required=REQUIRED)
    if test_names(invoke(["--ignored", "--list"])):
        raise RuntimeError("SCIM identity recovery contracts must not be ignored")
    require_passed(invoke(["--test-threads=1", "--nocapture"]), len(selected))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    run_contracts(parser.parse_args().source)
