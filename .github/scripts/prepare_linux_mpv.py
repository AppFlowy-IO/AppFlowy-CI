#!/usr/bin/env python3
"""Build the media runner's libmpv with the upstream PipeWire startup fix."""

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile

from macos_docker import checksum, download, inside


SOURCE_COMMIT = "41f6a645068483470267271e1d09966ca3b9f413"
FIX_COMMIT = "1a78f9fa8e6903bb51c9e43eefd64456a880ed13"
ASSETS = (
    {
        "name": "mpv-source.tar.gz",
        "url": f"https://codeload.github.com/mpv-player/mpv/tar.gz/{SOURCE_COMMIT}",
        "sha256": "068960e89211f2adc80af03f637fa393fb5e407d3dd23592c8fe8c5490bb7ae4",
    },
    {
        "name": "pipewire-startup.patch",
        "url": f"https://github.com/mpv-player/mpv/commit/{FIX_COMMIT}.patch",
        "sha256": "a0a53f189740c7d320e74bcbe1434b34fc8a4e9f71cff5205b0231a17aa32ec6",
    },
)


def build_runtime(root, fetch=download):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = inside(root, "manifest.json")
    manifest.unlink(missing_ok=True)
    assets = [fetch(asset, inside(root, "downloads")) for asset in ASSETS]
    for asset, path in zip(ASSETS, assets):
        if checksum(path) != asset["sha256"]:
            raise RuntimeError(f"SHA256 mismatch for {asset['name']}")

    install = inside(root, "install")
    if install.exists():
        shutil.rmtree(install)
    with tempfile.TemporaryDirectory(prefix="source-", dir=root) as temporary, \
            inside(root, "build.log").open("w") as log:
        staging = Path(temporary)
        with tarfile.open(assets[0]) as archive:
            archive.extractall(staging, filter="data")
        source = inside(staging, f"mpv-{SOURCE_COMMIT}")
        build = source / "build"
        # The distro release starts the loop while holding its lock. Backport
        # only the upstream ordering fix, retaining audio and video backends.
        commands = (
            (["patch", "--batch", "--fuzz=0", "-p1", "-i", str(assets[1])], 30),
            (["meson", "setup", str(build), str(source),
              f"--prefix={install}", "--libdir=lib", "-Dcplayer=false", "-Dlibmpv=true",
              "-Dbuild-date=false", "-Dmanpage-build=disabled",
              "-Dpdf-build=disabled", "-Dhtml-build=disabled"], 120),
            (["ninja", "-C", str(build), "-j2"], 420),
            (["ninja", "-C", str(build), "install"], 60),
        )
        for command, timeout in commands:
            log.write(shlex.join(command) + "\n")
            log.flush()
            subprocess.run(command, cwd=source, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=timeout)

        library = inside(install, "lib/libmpv.so.2")
        with library.open("rb") as binary:
            if binary.read(4) != b"\x7fELF":
                raise RuntimeError("The built libmpv is not an ELF library")
        linked = subprocess.run(["ldd", str(library)], text=True,
                                capture_output=True, check=True, timeout=30)
        log.write(linked.stdout)
        if "not found" in linked.stdout:
            raise RuntimeError("The built libmpv has unresolved runtime dependencies")

    result = {
        "source_commit": SOURCE_COMMIT,
        "upstream_fix": FIX_COMMIT,
        "source_sha256": ASSETS[0]["sha256"],
        "patch_sha256": ASSETS[1]["sha256"],
        "architecture": platform.machine(),
        "library": str(library),
        "library_sha256": checksum(library),
    }
    manifest.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    root = args.directory.resolve()
    if root == temporary or not root.is_relative_to(temporary):
        raise ValueError("The media runtime requires its own directory under RUNNER_TEMP")
    print(json.dumps(build_runtime(root)), flush=True)


if __name__ == "__main__":
    main()
