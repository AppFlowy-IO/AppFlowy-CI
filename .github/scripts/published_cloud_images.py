"""Resolve the Compose-selected AppFlowy dependency images once for this workflow."""

import json
import os
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
    locked = {}
    for name in services:
        image = model[name]["image"]
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise ValueError(f"No immutable image for {name}")
        locked[name] = {"image": image}
        print(f"{name}: {image}", flush=True)
    return locked


def main():
    command = ["docker", "compose", "--env-file", "deploy.env", "-f", "docker-compose-ci.yml",
               "config", "--no-env-resolution", "--format", "json"]
    services = json.loads(subprocess.check_output(command, text=True))["services"]
    lock = {"services": resolve(external_services(services))}
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print("lock=" + json.dumps(lock, separators=(",", ":")), file=output)


if __name__ == "__main__":
    main()
