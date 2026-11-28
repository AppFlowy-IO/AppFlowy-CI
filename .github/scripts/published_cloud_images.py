"""Resolve the Compose-selected AppFlowy dependency images once for this workflow."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


# These names are loaded from the private build's archives, including older Compose files
# that only declare an image. The legacy standalone AI fallback has its own release tag.
SOURCE_SERVICES = {"appflowy_cloud", "appflowy_worker", "appflowy_search", "appflowy_mcp",
                   "appflowy_backup", "ai"}


def external_services(services):
    return {name: {"image": service["image"]} for name, service in services.items()
            if name not in SOURCE_SERVICES and (name == "gateway_clickhouse" or
            service.get("image", "").removeprefix("docker.io/").startswith("appflowyinc/"))}


def resolve(services):
    if not services:
        return {}
    # Pass only image names to Compose. The private source's runtime env, mounts and
    # build contexts have no place in the lock or in the public workflow's outputs.
    command = ["docker", "compose", "-p", "ci-published-images", "-f", "-", "config",
               "--resolve-image-digests", "--format", "json"]
    model = json.loads(subprocess.check_output(command, text=True,
                       input=json.dumps({"services": services})))["services"]
    digests = {}
    for name in services:
        image = model[name]["image"]
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise ValueError(f"No immutable image for {name}")
        digests[name] = image.split("@")[1]
        print(f"{name}: {image}", flush=True)
    return digests


def compose_override(services, digests):
    if services.keys() != digests.keys():
        raise ValueError("Published service names differ from the resolved source Compose file")
    locked = {}
    for name, service in services.items():
        digest = digests[name]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid registry digest for {name}")
        image = service["image"].split("@")[0]
        locked[name] = {"image": f"{image}@{digest}"}
    return {"services": locked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--override", type=Path, help="write the Compose override from the run's digests")
    args = parser.parse_args()
    command = ["docker", "compose", "--env-file", "deploy.env", "-f", "docker-compose-ci.yml",
               "config", "--no-env-resolution", "--format", "json"]
    services = external_services(json.loads(subprocess.check_output(command, text=True))["services"])
    if args.override:
        digests = json.loads(os.environ["PUBLISHED_IMAGE_DIGESTS"])
        args.override.write_text(json.dumps(compose_override(services, digests)))
    else:
        # Registry namespaces can match GitHub's masked login username. Only digests
        # cross job boundaries. Consumers already have the same immutable Compose source.
        digests = resolve(services)
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            print("digests=" + json.dumps(digests, separators=(",", ":")), file=output)


if __name__ == "__main__":
    main()
