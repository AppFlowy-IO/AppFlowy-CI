"""Validate the CI-owned Intel Docker VM without building or testing Desktop."""

import json
import os
from pathlib import Path
import socket
import subprocess

from setup_macos_docker import require_intel_runner


# Official Redis 7.4.12 Alpine linux/amd64 manifest, verified from Docker Hub.
IMAGE = "redis@sha256:1cf73a2f04808e55a8bd2f5706c2aa0ebb0f71938f93deb7af79f48311cd5cb9"


def smoke(directory, project):
    directory.mkdir(mode=0o700)
    compose_file = directory / "compose.json"
    compose_file.write_text(json.dumps({"services": {"redis": {
        "image": IMAGE,
        "platform": "linux/amd64",
        "ports": ["127.0.0.1::6379"],
        "healthcheck": {"test": ["CMD", "redis-cli", "ping"],
                        "interval": "1s", "timeout": "1s", "retries": 30},
    }}}))
    compose = ["docker", "compose", "--project-name", project, "--file", str(compose_file)]
    try:
        subprocess.run([*compose, "up", "--detach", "--wait", "--wait-timeout", "60"],
                       check=True, timeout=120)
        architecture = subprocess.check_output(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Os}}/{{.Architecture}}"],
            text=True, timeout=10).strip()
        if architecture != "linux/amd64":
            raise ValueError(f"Expected an Intel Linux container, got {architecture}")
        address = subprocess.check_output([*compose, "port", "redis", "6379"],
                                          text=True, timeout=10).strip()
        host, port = address.rsplit(":", 1)
        if host != "127.0.0.1":
            raise ValueError("Smoke service must be published on loopback only")
        with socket.create_connection((host, int(port)), timeout=5) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            with connection.makefile("rb") as stream:
                response = stream.readline(64)
        if response != b"+PONG\r\n":
            raise ValueError("Docker VM did not answer the host's Redis PING")
        print("Intel Linux container, Compose healthcheck and host-to-VM PING passed")
    finally:
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"],
                       check=True, timeout=30)


def main():
    require_intel_runner()
    smoke(Path(os.environ["RUNNER_TEMP"]) / "mobile-docker-smoke",
          f"mobile-docker-smoke-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}")


if __name__ == "__main__":
    main()
