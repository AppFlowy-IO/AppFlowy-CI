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
        "LIMA_HOME": str(root / "colima/_lima"),
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
        for name in ("DOCKER_CONFIG", "COLIMA_HOME", "LIMA_HOME"):
            output.write(f"{name}={values[name]}\n")


def owned_log_tail(root, path):
    try:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            return None
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - 64 * 1024))
            return source.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None


def collect_runtime_diagnostics(root, destination):
    # A failed pull can leave Docker responsive while its network or disk work
    # stops. Keep evidence before the ephemeral VM and its logs are discarded.
    if not os.environ.get("RUNNER_TEMP"):
        raise ValueError("Runtime diagnostics require RUNNER_TEMP")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    root, destination = root.resolve(), destination.resolve()
    for path in (root, destination):
        if path == temporary or not path.is_relative_to(temporary):
            raise ValueError("Runtime diagnostics require job-owned temporary paths")
    inside(root, "colima/_lima")
    socket = inside(root, "colima/default/docker.sock")
    binary = inside(root, "tools/bin/docker")
    lima = inside(root, "tools/bin/limactl")
    destination.mkdir(parents=True, exist_ok=True)
    values = environment(root)
    values.pop("DOCKER_CONTEXT", None)
    values["DOCKER_HOST"] = f"unix://{socket}"
    client = [str(binary), "--host", values["DOCKER_HOST"]]
    commands = {
        "docker-info.log": client + ["info", "--format", '{"server":{{json .ServerVersion}},'
                                     '"cpus":{{json .NCPU}},"memory":{{json .MemTotal}},'
                                     '"driver":{{json .Driver}}}'],
        "docker-images.log": client + ["image", "ls", "--digests"],
        "docker-storage.log": client + ["system", "df"],
        "guest-kernel.log": [str(lima), "shell", "colima", "sudo", "-n", "dmesg", "--level=err,warn"],
        "guest-storage.log": [str(lima), "shell", "colima", "df", "-h", "/",
                              "/var/lib/docker", "/var/lib/containerd"],
        "guest-runtime.log": [str(lima), "shell", "colima", "sudo", "-n", "journalctl",
                              "--no-pager", "-n", "300", "-u", "docker", "-u", "containerd"],
        "host-memory.log": ["vm_stat"],
        "host-storage.log": ["df", "-h", str(root)],
    }
    results = []
    for name, command in commands.items():
        result = {"command": command, "log": name}
        with tempfile.TemporaryFile(dir=destination) as output:
            try:
                process = subprocess.run(command, env=values, stdout=output,
                                         stderr=subprocess.STDOUT, timeout=10, check=False)
                result["exit_code"] = process.returncode
            except (OSError, subprocess.SubprocessError) as error:
                result["error"] = type(error).__name__
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 64 * 1024))
            inside(destination, name).write_bytes(output.read(64 * 1024))
        results.append(result)
    logs = root / "colima/_lima"
    retained = 0
    for pattern in ("*/serial.log", "*/serial0.log", "*/ha.stderr.log", "_networks/*/*.stderr.log"):
        for path in logs.glob(pattern):
            if retained == 8:
                break
            tail = owned_log_tail(root, path)
            if tail is not None:
                name = "lima-" + str(path.relative_to(logs)).replace("/", "-")
                inside(destination, name).write_bytes(tail.encode("utf-8")[-64 * 1024:])
                retained += 1
    inside(destination, "summary.json").write_text(json.dumps({"commands": results}, indent=2) + "\n")


def configure_image_store(root):
    # Containerd's image store retains compressed layers as well as extracted
    # snapshots. Cold pulls stalled during extraction on this small CI VM.
    # Keep Colima's generated networking/runtime configuration, and change only
    # this fresh VM's store before any images or containers exist.
    values = environment(root)
    values.pop("DOCKER_CONTEXT", None)
    values["DOCKER_HOST"] = f"unix://{inside(root, 'colima/default/docker.sock')}"
    guest = [str(inside(root, "tools/bin/limactl")), "shell", "colima", "sudo", "-n"]
    config_path = "/etc/docker/daemon.json"
    config = json.loads(subprocess.check_output(
        guest + ["cat", config_path], env=values, text=True, timeout=30,
    ))
    if not isinstance(config, dict) or not isinstance(config.get("features", {}), dict):
        raise ValueError("The owned VM's Docker configuration must contain objects")
    config.setdefault("features", {})["containerd-snapshotter"] = False
    subprocess.run(guest + ["tee", config_path], input=json.dumps(config) + "\n",
                   env=values, text=True, capture_output=True, check=True, timeout=30)
    subprocess.run(guest + ["systemctl", "restart", "docker"], env=values,
                   check=True, timeout=60)
    driver = subprocess.check_output(
        [str(inside(root, "tools/bin/docker")), "--host", values["DOCKER_HOST"],
         "info", "--format", "{{.Driver}}"], env=values, text=True, timeout=30,
    ).strip()
    if driver != "overlay2":
        raise RuntimeError(f"The owned Docker VM must use overlay2; reported {driver!r}")
    print(f"Docker image storage driver: {driver}", flush=True)


def start_runtime(root):
    # Colima ignores COLIMA_HOME unless the directory already exists. Otherwise
    # a fresh job silently uses ~/.colima, including any inherited Lima profile.
    (root / "colima").mkdir(parents=True, exist_ok=True)
    # The simulator and macOS must remain responsive while the Cloud VM runs.
    # The failed hosted start requested all 14 GiB; reserve host headroom.
    cpus = int(subprocess.check_output(["sysctl", "-n", "hw.ncpu"], text=True))
    memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) // (1024 ** 3)
    cpus = max(1, min(2, cpus - 1))
    memory = max(2, min(8, memory - 6))
    try:
        subprocess.run([
            str(root / "tools/bin/colima"), "start", "--cpu", str(cpus), "--memory", str(memory),
            "--arch", "x86_64", "--vm-type=vz", "--mount-type=virtiofs",
        ], env=environment(root), check=True, timeout=600)
    except subprocess.SubprocessError:
        # The SSH timeout alone cannot distinguish a guest boot failure from
        # host pressure. Retain bounded boot evidence without reading config.
        logs = root / "colima/_lima/colima"
        for name in ("serial.log", "serial0.log", "ha.stderr.log"):
            tail = owned_log_tail(root, logs / name)
            if tail is not None:
                print(f"Colima boot diagnostic ({name}):\n{tail}", file=sys.stderr, flush=True)
        raise
    # A working client alone must never count as a ready Cloud runtime.
    configure_image_store(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "start", "diagnose"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.operation == "diagnose" and args.output is None:
        parser.error("diagnose requires --output")
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
        elif args.operation == "start":
            start_runtime(root)
        else:
            collect_runtime_diagnostics(root, args.output)
    except (OSError, RuntimeError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"macOS Docker setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
