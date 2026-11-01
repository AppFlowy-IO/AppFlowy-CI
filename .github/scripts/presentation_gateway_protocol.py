#!/usr/bin/env python3
"""Pair each source-owned Cloud bridge protocol with a reviewed Service revision."""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class GatewayProtocol:
    service_sha: str
    completion_marker: str


LEGACY = GatewayProtocol(
    "44bcd0f16baa75d95d8d4c2ea873cd246498738a",
    "API02 actual Gateway router -> HostedPresentationUsage -> Cloud HTTP -> AppResponse: PASS",
)
INGRESS = GatewayProtocol(
    "c1e15d6698dbfa604e24453114f923cd1fbe955f",
    "API02 actual Gateway ingress -> standalone Cloud HTTP -> durable job reconciliation: PASS",
)


def gateway_protocol(source):
    runner = Path(source) / "script/test_presentation_gateway_contract.py"
    if not runner.is_file():
        raise RuntimeError("Cloud is missing its source-owned Gateway contract runner")
    # The ingress runner publishes Cloud's listener through this explicit rendezvous file.
    # Older Cloud instead calls the signed accounting endpoint and needs the legacy fixture.
    return INGRESS if "PRESENTATION_CLOUD_BRIDGE_ADDRESS" in runner.read_text() else LEGACY


def service_revision(source, override=None):
    protocol = gateway_protocol(source)
    revision = override or protocol.service_sha
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise RuntimeError("PRESENTATION_SERVICE_SHA must be a reviewed immutable Service commit")
    return revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args()
    revision = service_revision(args.source, os.environ.get("PRESENTATION_SERVICE_SHA"))
    with args.github_env.open("a", encoding="utf-8") as output:
        print(f"PRESENTATION_SERVICE_SHA={revision}", file=output)


if __name__ == "__main__":
    main()
