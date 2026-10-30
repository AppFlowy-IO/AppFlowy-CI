"""Install verified upstream binaries, never Homebrew source builds, on CI's Intel Mac."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request


MANIFEST = Path(__file__).with_name("macos_docker_tools.json")


def fetch(url, destination):
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("xb") as output:
        shutil.copyfileobj(response, output)


def download_tools(root, manifest, fetch_asset=fetch):
    """Verify every executable before installing or publishing any tool path."""
    root.mkdir(mode=0o700)
    downloads = root / "downloads"
    downloads.mkdir()
    assets = {}
    for name, entry in manifest.items():
        asset = downloads / name
        fetch_asset(entry["url"], asset)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"SHA256 mismatch for {name}; no tools were installed")
        assets[name] = asset
    return assets


def install_tools(root, assets):
    prefix = root / "tools"
    prefix.mkdir()
    # These are pinned upstream archives, verified before extraction. Lima's
    # bin/share/libexec layout must stay together for its guest agent lookup.
    subprocess.run(["tar", "-xzf", str(assets["lima"]), "-C", str(prefix)], check=True)
    binaries = prefix / "bin"
    binaries.mkdir(exist_ok=True)
    with tarfile.open(assets["docker"], "r:gz") as archive:
        with archive.extractfile("docker/docker") as source:
            (binaries / "docker").write_bytes(source.read())
    shutil.copyfile(assets["colima"], binaries / "colima")
    plugins = root / "docker" / "cli-plugins"
    plugins.mkdir(parents=True)
    shutil.copyfile(assets["compose"], plugins / "docker-compose")
    shutil.copyfile(assets["buildx"], plugins / "docker-buildx")
    for binary in (binaries / "docker", binaries / "colima", *plugins.iterdir()):
        binary.chmod(0o755)
    return prefix


def runner_environment(root, prefix):
    return {
        **os.environ,
        "PATH": f"{prefix / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_CONFIG": str(root / "docker"),
        "COLIMA_HOME": str(root / "colima"),
        "LIMA_HOME": str(root / "lima"),
    }


def start_runtime(root, prefix):
    environment = runner_environment(root, prefix)
    host_memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) // (1024 ** 3)
    # Xcode and CoreSimulator remain on the host. Giving their entire physical
    # memory to a second OS makes the cold VM compete with the native tests.
    memory = min(8, host_memory // 2)
    if memory < 2:
        raise ValueError("The Intel Docker runner needs at least 4 GiB of host memory")
    print(f"Docker VM memory: {memory} GiB; host physical memory: {host_memory} GiB", flush=True)
    subprocess.run([
        "colima", "start", "--cpu", str(os.cpu_count()), "--memory", str(memory),
        "--arch", "x86_64", "--vm-type=vz", "--mount-type=virtiofs",
    ], env=environment, check=True)
    for command in (
        ["docker", "info"], ["docker", "compose", "version"],
        ["docker", "buildx", "version"], ["colima", "version"],
    ):
        subprocess.run(command, env=environment, check=True)
    # Downstream login/runtime/cleanup steps use this job-owned configuration.
    # A failed install/start never publishes a half-working client or context.
    with Path(os.environ["GITHUB_PATH"]).open("a") as output:
        output.write(f"{prefix / 'bin'}\n")
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        for name in ("DOCKER_CONFIG", "COLIMA_HOME", "LIMA_HOME"):
            output.write(f"{name}={environment[name]}\n")


def require_intel_runner():
    if not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_OS") == "macOS"
        and os.environ.get("RUNNER_ARCH") == "X64"
        and platform.system() == "Darwin"
        and platform.machine() == "x86_64"
    ):
        raise ValueError("Docker setup requires a GitHub-owned Intel macOS runner")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-downloads", type=Path,
                        help="Only download/verify into a new directory; never install or start a VM")
    arguments = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    if arguments.verify_downloads:
        assets = download_tools(arguments.verify_downloads, manifest)
        print(f"Verified {len(assets)} pinned upstream artifacts; no tools or VM were started")
        return
    require_intel_runner()
    root = Path(os.environ["RUNNER_TEMP"]) / "mobile-docker-tools"
    assets = download_tools(root, manifest)
    prefix = install_tools(root, assets)
    start_runtime(root, prefix)


if __name__ == "__main__":
    main()
