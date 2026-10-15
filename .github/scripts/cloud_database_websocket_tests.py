#!/usr/bin/env python3
"""Run database WebSocket loss regressions with history disabled in the CI stack."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


TEST_PREFIX = "collab::missing_user_updates::database::"
REQUIRED_TESTS = {
    TEST_PREFIX + name for name in (
        "database_missing_middle_update_recovers_through_sdk",
        "database_outbound_updates_lost_recover_through_sdk",
        "database_row_missing_middle_update_recovers_through_sdk",
        "database_row_outbound_updates_lost_recover_through_sdk",
    )
}


def test_names(output):
    return {line.removesuffix(": test") for line in output.splitlines() if line.endswith(": test")}


def verify_inventory(selected, ignored):
    names = test_names(selected)
    if (not REQUIRED_TESTS.issubset(names) or test_names(ignored)
            or not all(name.startswith(TEST_PREFIX) for name in names)):
        raise RuntimeError("Database WebSocket loss regressions are missing, ignored or misselected")


@contextmanager
def websocket_compose():
    # Older Cloud sources hardcode history=true. A temporary service override supports both
    # those sources and the newer CI environment knob without changing ordinary test settings.
    with TemporaryDirectory(prefix="appflowy-ci-websocket-") as directory:
        override = Path(directory) / "compose.json"
        override.write_text(json.dumps({"services": {
            "appflowy_cloud": {"environment": {"APPFLOWY_DATABASE_HISTORY_ENABLED": "false"}},
        }}))
        yield ["docker", "compose", "-f", "docker-compose-ci.yml", "-f", str(override)]


def wait_for_server(base_url, timeout=120):
    deadline = time.monotonic() + timeout
    observed = "server unavailable"
    # Unknown/old clients see history=false even when enabled. Use the native headers that
    # expose the real transport policy so a stale server cannot pass this readiness check.
    request = Request(base_url.rstrip("/") + "/api/server-info", headers={
        "client-version": "0.14.6", "x-platform": "app",
    })
    while time.monotonic() < deadline:
        try:
            with urlopen(request, timeout=5) as response:
                envelope = json.load(response)
            history = (envelope.get("data") or {}).get("enable_database_history")
            if envelope.get("code") == 0 and history is False:
                return
            observed = f"history={history}"
        except (URLError, TimeoutError, OSError, ValueError) as error:
            observed = type(error).__name__
        time.sleep(1)
    raise RuntimeError(f"Server did not disable database history: {observed}")


def run_tests(features):
    runner = Path(__file__).with_name("cloud_test_binaries.py")
    command = [sys.executable, str(runner), "run", "test", "--locked", "--test", "main",
               "--features", features, TEST_PREFIX, "--"]
    selected = subprocess.check_output([*command, "--list"], text=True)
    ignored = subprocess.check_output([*command, "--ignored", "--list"], text=True)
    verify_inventory(selected, ignored)

    environment = {
        **os.environ,
        "APPFLOWY_CLOUD_VERSION": "latest-amd64",
        "RUST_LOG": "info",
        "DISABLE_CI_TEST_LOG": "true",
    }
    with websocket_compose() as compose:
        # Resolved Compose settings can contain credentials; inspect without logging them.
        configuration = json.loads(subprocess.check_output(
            [*compose, "config", "--format", "json"], env=environment, text=True,
        ))
        history = configuration["services"]["appflowy_cloud"]["environment"].get(
            "APPFLOWY_DATABASE_HISTORY_ENABLED"
        )
        if history != "false":
            raise RuntimeError("Cloud Compose configuration must disable database history")
        print(f"Running {len(test_names(selected))} database WebSocket loss tests with history disabled",
              flush=True)
        subprocess.run(
            [*compose, "up", "-d", "--no-deps", "--no-build", "--pull=never", "--force-recreate",
             "--wait", "--wait-timeout", "120", "appflowy_cloud"],
            env=environment, check=True,
        )
        wait_for_server(environment.get("LOCALHOST_URL", "http://localhost"))
        subprocess.run([*command, "--test-threads=1"], env=environment, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True)
    arguments = parser.parse_args()
    run_tests(arguments.features)
