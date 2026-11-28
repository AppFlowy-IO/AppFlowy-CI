"""Pull pinned dependencies on the runner with temporary Docker Hub credentials."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def references(lock, selected):
    services = lock["services"]
    refs = []
    for name in selected or services:
        record = services[name]
        image = record["image"]
        if (set(record) != {"image", "pull_policy"} or record["pull_policy"] != "never" or
                not re.fullmatch(r"(?:docker\.io/)?(?:appflowyinc/[a-z0-9_.-]+|clickhouse/clickhouse-server)"
                                 r"(?::[\w.-]+)?@sha256:[0-9a-f]{64}", image)):
            raise ValueError("Unexpected private dependency manifest entry")
        refs.append(image)
    return refs


def pull_images(images, username, token):
    # Docker writes its login to this directory. Only trusted pulls run while it exists.
    # Child processes receive no registry secret variables. Never log Docker diagnostics
    # containing registry references, bearer URLs or authentication material.
    env = {key: os.environ[key] for key in ("HOME", "PATH", "RUNNER_TEMP") if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="cloud-registry-", dir=env["RUNNER_TEMP"]) as config:
        docker = ["docker", "--config", config]
        def invoke(command, stdin=None):
            result = subprocess.run(docker + command, input=stdin, text=True, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            if result.returncode:
                raise RuntimeError("Authenticated dependency preparation failed")
        Path(config, "config.json").write_text('{"auths":{"https://index.docker.io/v1/":{}}}')
        try:
            invoke(["login", "--username", username, "--password-stdin"], token + "\n")
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda image: invoke(["pull", "--quiet", image]), images))
        finally:
            subprocess.run(docker + ["logout"], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("services", nargs="*")
    args = parser.parse_args()
    username = os.environ.pop("REGISTRY_USER")
    token = os.environ.pop("REGISTRY_TOKEN")
    refs = references(json.loads(args.lock.read_text()), args.services)
    if os.environ.get("AI_IMAGE_FROM_SOURCE") == "false" and not args.services:
        # Historical sources use a fixed standalone release when the private builder has no AI archive.
        env = dict(os.environ, APPFLOWY_AI_VERSION="0.13.2-amd64")
        data = subprocess.check_output(["docker", "compose", "--env-file", "deploy.env", "-f",
                                       "docker-compose-ci.yml", "config", "--no-env-resolution",
                                       "--format", "json"], env=env, stderr=subprocess.DEVNULL)
        ai = json.loads(data)["services"].get("ai")
        if ai:
            image = ai["image"]
            if not re.fullmatch(r"(?:docker\.io/)?appflowyinc/[a-z0-9_.-]+:0\.13\.2-amd64", image):
                raise ValueError("Unexpected historical AI release")
            refs.append(image)
    pull_images(refs, username, token)
    print(f"Prepared {len(refs)} published dependencies on the ephemeral runner.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise SystemExit("::error::Could not prepare the private dependency images")
