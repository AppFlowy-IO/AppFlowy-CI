#!/usr/bin/env python3
"""Install pinned Docker tools and start the Intel macOS CI runtime without Homebrew."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


MANIFEST = Path(__file__).with_name("macos_docker_tools.json")


def require_intel_macos():
    if platform.system() != "Darwin" or platform.machine() != "x86_64":
        raise RuntimeError("The Cloud iOS runtime requires an Intel macOS runner with VZ support.")


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(asset, directory):
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / asset["name"]
    if archive.exists() and checksum(archive) == asset["sha256"]:
        return archive
    archive.unlink(missing_ok=True)
    subprocess.run([
        "curl", "--fail", "--location", "--silent", "--show-error",
        "--connect-timeout", "15", "--max-time", "120", "--retry", "2",
        "--retry-max-time", "300", "--output", str(archive), asset["url"],
    ], check=True, timeout=330)
    if checksum(archive) != asset["sha256"]:
        archive.unlink()
        raise RuntimeError(f"SHA256 mismatch for {asset['name']}")
    return archive


def inside(root, relative):
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Tool destination is outside its job-owned prefix: {relative}")
    return result


def install_tools(root, assets, fetch=download):
    root.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        archive = fetch(asset, root / "downloads")
        # Verify at the installation boundary too, including cached downloads.
        if checksum(archive) != asset["sha256"]:
            raise RuntimeError(f"SHA256 mismatch for {asset['name']}")
        destination = inside(root, asset["destination"])
        if asset["format"] == "tar":
            if "member" in asset:
                with tempfile.TemporaryDirectory(dir=root) as directory:
                    staging = Path(directory)
                    with tarfile.open(archive) as bundle:
                        bundle.extractall(staging, filter="data")
                    source = inside(staging, asset["member"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            else:
                destination.mkdir(parents=True, exist_ok=True)
                with tarfile.open(archive) as bundle:
                    bundle.extractall(destination, filter="data")
                # Lima's relocatable distribution includes libexec and guest agents.
                destination = destination / "bin/limactl"
        elif asset["format"] == "file":
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, destination)
        else:
            raise ValueError(f"Unsupported tool archive format: {asset['format']}")
        destination.chmod(0o755)


def environment(root):
    return {
        **os.environ,
        "PATH": str(root / "tools/bin") + os.pathsep + os.environ["PATH"],
        "DOCKER_CONFIG": str(root / "docker"),
        "COLIMA_HOME": str(root / "colima"),
    }


def verify_macos_version(binary, system_version):
    metadata = subprocess.check_output(
        ["xcrun", "vtool", "-show-build", str(binary)], text=True, timeout=30,
    )
    minimum = re.search(r"^\s*minos\s+([\d.]+)", metadata, re.M)
    if minimum is None:
        minimum = re.search(r"^\s*version\s+([\d.]+)", metadata, re.M)
    if minimum is None:
        raise RuntimeError(f"No macOS deployment target reported for {binary.name}")

    def version(value):
        return tuple((list(map(int, value.split("."))) + [0, 0])[:3])

    if version(minimum[1]) > version(system_version):
        raise RuntimeError(
            f"{binary.name} requires macOS {minimum[1]}; runner is macOS {system_version}",
        )


def verify_versions(root, assets):
    versions = {asset["name"]: asset["version"] for asset in assets}
    commands = {
        "docker": [str(root / "tools/bin/docker"), "--version"],
        "compose": [str(root / "tools/bin/docker"), "compose", "version"],
        "buildx": [str(root / "tools/bin/docker"), "buildx", "version"],
        "lima": [str(root / "tools/bin/limactl"), "--version"],
        "colima": [str(root / "tools/bin/colima"), "version"],
    }
    for binary in (
        root / "tools/bin/docker", root / "tools/bin/limactl", root / "tools/bin/colima",
        root / "docker/cli-plugins/docker-compose", root / "docker/cli-plugins/docker-buildx",
    ):
        verify_macos_version(binary, platform.mac_ver()[0])
    for name, command in commands.items():
        result = subprocess.run(command, env=environment(root), check=True,
                                text=True, capture_output=True, timeout=30)
        if versions[name] not in result.stdout:
            raise RuntimeError(f"Installed {name} does not report pinned version {versions[name]}")
        print(result.stdout.strip(), flush=True)


def publish_environment(root):
    values = environment(root)
    with Path(os.environ["GITHUB_PATH"]).open("a") as output:
        output.write(str(root / "tools/bin") + "\n")
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        for name in ("DOCKER_CONFIG", "COLIMA_HOME"):
            output.write(f"{name}={values[name]}\n")


def start_runtime(root):
    # Keep the upstream action's VZ backend, architecture and resource sizing.
    cpus = int(subprocess.check_output(["sysctl", "-n", "hw.ncpu"], text=True))
    memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) // (1024 ** 3)
    subprocess.run([
        str(root / "tools/bin/colima"), "start", "--cpu", str(cpus), "--memory", str(memory),
        "--arch", "x86_64", "--vm-type=vz", "--mount-type=virtiofs",
    ], env=environment(root), check=True, timeout=600)
    # Colima selects its Docker context in the job-owned DOCKER_CONFIG.
    # A working client alone must never count as a ready Cloud runtime.
    subprocess.run([str(root / "tools/bin/docker"), "info"], env=environment(root),
                   check=True, timeout=30)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "start"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require_intel_macos()
        root = args.root.resolve()
        if args.operation == "install":
            manifest = json.loads(MANIFEST.read_text())
            assets = manifest["artifacts"]
            if {asset["name"] for asset in assets} != {"lima", "colima", "docker", "compose", "buildx"}:
                raise ValueError("The pinned Docker tool set is incomplete")
            install_tools(root, assets)
            verify_versions(root, assets)
            publish_environment(root)
        else:
            start_runtime(root)
    except (OSError, RuntimeError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"macOS Docker setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
