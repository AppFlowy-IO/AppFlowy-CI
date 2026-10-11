#!/usr/bin/env python3
"""Run tests with conflicting server requirements after their topic's ordinary tests.

The job owns a disposable Compose stack. Recreate only its affected services, preserving the
snapshot and precompiled test binaries. Verify discovery and the effective configuration before
running assertions; a missing or ignored regression must not become a successful empty selection.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class FixtureMode:
    test: str
    minimum: int
    history: bool
    view_limit: int
    services: tuple[str, ...]
    exact: bool = False


MODES = {
    "database-websocket-loss": FixtureMode(
        "collab::missing_user_updates::database::", 4, False, 6, ("appflowy_cloud",),
    ),
    "legacy-duplication": FixtureMode(
        "workspace::page_view::duplication_test::"
        "duplicate_export_user_document_preserves_embedded_docs",
        1, True, 10, ("appflowy_cloud", "appflowy_worker"), exact=True,
    ),
}


def test_names(output):
    return [line.removesuffix(": test") for line in output.splitlines() if line.endswith(": test")]


def verify_inventory(mode, selected, ignored):
    names = test_names(selected)
    matches = (lambda name: name == mode.test) if mode.exact else (
        lambda name: name.startswith(mode.test)
    )
    if (len(names) < mode.minimum or (mode.exact and len(names) != 1)
            or not all(matches(name) for name in names) or test_names(ignored)):
        raise RuntimeError(
            f"Invalid fixture selection {mode.test}: {len(names)} selected, "
            f"{len(test_names(ignored))} ignored; require at least {mode.minimum} active tests"
        )


def verify_compose(mode, configuration):
    services = configuration["services"]
    history = str(mode.history).lower()
    if services["appflowy_cloud"]["environment"]["APPFLOWY_DATABASE_HISTORY_ENABLED"] != history:
        raise RuntimeError("Cloud Compose configuration does not match the fixture's database-history setting")
    for service in mode.services:
        if services[service]["environment"]["APPFLOWY_MAX_DASHBOARD_WIDGETS"] != str(mode.view_limit):
            raise RuntimeError(f"{service} does not honor the fixture's database view limit")


@contextmanager
def fixture_compose(mode):
    # Older Cloud sources hardcode history=true. Override only this phase's writers,
    # without editing their source Compose file or changing the ordinary test phase.
    services = {
        service: {"environment": {"APPFLOWY_MAX_DASHBOARD_WIDGETS": str(mode.view_limit)}}
        for service in mode.services
    }
    services["appflowy_cloud"]["environment"]["APPFLOWY_DATABASE_HISTORY_ENABLED"] = (
        str(mode.history).lower()
    )
    with TemporaryDirectory(prefix="appflowy-ci-fixture-") as directory:
        override = Path(directory) / "compose.json"
        override.write_text(json.dumps({"services": services}))
        yield ["docker", "compose", "-f", "docker-compose-ci.yml", "-f", str(override)]


def wait_for_server(mode, base_url, timeout=120):
    deadline = time.monotonic() + timeout
    observed = "server unavailable"
    # Server-info deliberately hides history from unknown/old clients. Use a native client
    # version that supports database restore, otherwise even a history-enabled server says false.
    request = Request(base_url.rstrip("/") + "/api/server-info", headers={
        "client-version": "0.14.6", "x-platform": "app",
    })
    while time.monotonic() < deadline:
        try:
            with urlopen(request, timeout=5) as response:
                envelope = json.load(response)
            info = envelope.get("data") or {}
            if (envelope.get("code") == 0 and info.get("enable_database_history") is mode.history
                    and info.get("max_dashboard_widgets") == mode.view_limit):
                return
            observed = (f"history={info.get('enable_database_history')}, "
                        f"view_limit={info.get('max_dashboard_widgets')}")
        except (URLError, TimeoutError, OSError, ValueError) as error:
            observed = type(error).__name__
        time.sleep(1)
    raise RuntimeError(f"Server did not enter the requested fixture configuration: {observed}")


def run_mode(mode, features):
    runner = Path(__file__).with_name("cloud_test_binaries.py")
    command = [sys.executable, str(runner), "run", "test", "--locked", "--test", "main",
               "--features", features, mode.test, "--"]
    if mode.exact:
        command.append("--exact")
    selected = subprocess.check_output([*command, "--list"], text=True)
    ignored = subprocess.check_output([*command, "--ignored", "--list"], text=True)
    verify_inventory(mode, selected, ignored)

    environment = {
        **os.environ,
        "APPFLOWY_CLOUD_VERSION": "latest-amd64",
        "APPFLOWY_WORKER_VERSION": "latest-amd64",
        "RUST_LOG": "info",
        "DISABLE_CI_TEST_LOG": "true",
    }
    with fixture_compose(mode) as compose:
        # Keep resolved environment values (including credentials) out of the public job log.
        configuration = json.loads(subprocess.check_output(
            [*compose, "config", "--format", "json"], env=environment, text=True,
        ))
        verify_compose(mode, configuration)
        print(f"Running {len(test_names(selected))} tests with history={mode.history}, "
              f"view_limit={mode.view_limit}", flush=True)
        subprocess.run(
            [*compose, "up", "-d", "--no-deps", "--no-build", "--pull=never", "--force-recreate",
             "--wait", "--wait-timeout", "120", *mode.services],
            env=environment, check=True,
        )
        wait_for_server(mode, environment.get("LOCALHOST_URL", "http://localhost"))
        test_environment = {**environment, "APPFLOWY_MAX_DASHBOARD_WIDGETS": str(mode.view_limit)}
        subprocess.run([*command, "--test-threads=1"], env=test_environment, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--features", required=True)
    arguments = parser.parse_args()
    run_mode(MODES[arguments.mode], arguments.features)
