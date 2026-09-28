"""Resolve one published Cloud release for the Flutter integration stack.

Cloud owns SQL migrations. A failed Cloud build must not pair its previous
release with newer Workers that independently advanced their latest tag.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess


REPOSITORIES = {
    "appflowy_cloud": "appflowyinc/appflowy_cloud_premium",
    "appflowy_worker": "appflowyinc/appflowy_worker_premium",
    "appflowy_search": "appflowyinc/appflowy_search_premium",
    "appflowy_mcp": "appflowyinc/appflowy_mcp_premium",
}
LABEL_PREFIX = "org.opencontainers.image."


def inspect_image(reference):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference,
         "--format", "{{json .}}"],
        check=True, capture_output=True, text=True, timeout=120,
    )
    return json.loads(result.stdout)


def image_identity(metadata):
    labels = metadata["image"]["config"]["Labels"]
    version = labels[LABEL_PREFIX + "version"]
    revision = labels[LABEL_PREFIX + "revision"]
    digest = metadata["manifest"]["digest"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", version):
        raise ValueError(f"Invalid published Cloud version: {version!r}")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError(f"Missing full source revision: {revision!r}")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError(f"Invalid image digest: {digest!r}")
    return version, revision, digest


def select_images(cloud_tag, inspect=inspect_image):
    cloud = inspect(f"{REPOSITORIES['appflowy_cloud']}:{cloud_tag}")
    version, revision, _ = image_identity(cloud)
    services = {}
    for service, repository in REPOSITORIES.items():
        metadata = cloud if service == "appflowy_cloud" else inspect(
            f"{repository}:{version}-amd64"
        )
        actual_version, actual_revision, digest = image_identity(metadata)
        if (actual_version, actual_revision) != (version, revision):
            raise ValueError(
                f"{service} is {actual_version} ({actual_revision}); "
                f"Cloud requires {version} ({revision})"
            )
        services[service] = {"image": f"{repository}@{digest}"}
    return revision, {"services": services}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    revision, compose = select_images(args.cloud_tag)
    # Publish only a complete, verified set. Compose consumes immutable digests
    # so subsequent latest-tag changes cannot split the running services.
    args.output.write_text(json.dumps(compose, indent=2) + "\n")
    with args.github_output.open("a") as output:
        output.write(f"revision={revision}\n")
    print(f"Selected Cloud backend source {revision}")
    print(json.dumps(compose, indent=2))


if __name__ == "__main__":
    main()
