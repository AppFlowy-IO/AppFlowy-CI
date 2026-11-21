"""Configure package downloads on disposable GitHub-hosted Ubuntu runners."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile


SLOW_RUNNER_ARCHIVE = re.compile(
    r"https?://(?:azure|us-east-2\.ec2)\.archive\.ubuntu\.com(?=/ubuntu(?:/|\s|$))"
)
FALLBACK_MIRRORS = (
    ("https://mirrors.edge.kernel.org/ubuntu/", 20),
    ("https://ftp.osuosl.org/pub/ubuntu/", 21),
)
# Use a dedicated archive directory so image cleanup hooks for the system
# cache cannot discard the packages before the preparation job uploads them.
ARCHIVES = Path("/var/cache/appflowy-flutter-apt")
NETWORK_CONFIG = """// Bound package downloads on GitHub-hosted runners.
Acquire::Retries "1";
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
"""
ARCHIVE_CONFIG = f'Dir::Cache::archives "{ARCHIVES}";\nAPT::Keep-Downloaded-Packages "true";\n'


def configure_apt(apt_root: Path, *, keep_archives: bool = True) -> int:
    # Keep suites, components, signatures and third-party repositories intact.
    # Both Azure and the us-east-2 EC2 mirror have exhausted the runner setup
    # deadline during package downloads. New runner images select their preferred
    # archive via mirror+file, while older images embed it in the APT source.
    sources = [apt_root / "sources.list", apt_root / "apt-mirrors.txt"]
    for extension in ("*.list", "*.sources"):
        sources.extend(sorted((apt_root / "sources.list.d").glob(extension)))

    changed = 0
    for source in sources:
        if not source.is_file():
            continue
        original = source.read_text()
        updated = SLOW_RUNNER_ARCHIVE.sub("https://archive.ubuntu.com", original)
        if source.name == "apt-mirrors.txt":
            urls = {line.split()[0].rstrip("/") for line in updated.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")}
            if any(re.fullmatch(r"https?://(?:archive|security)\.ubuntu\.com/ubuntu", url)
                   for url in urls):
                # APT tries the next mirror on missing packages and connection errors. Keep the
                # image's existing preferences and let APT verify Ubuntu's signatures and hashes.
                missing = [f"{url}\tpriority:{priority}\n" for url, priority in FALLBACK_MIRRORS
                           if url.rstrip("/") not in urls]
                if missing:
                    updated = updated.rstrip("\n") + "\n" + "".join(missing)
        if updated != original:
            source.write_text(updated)
            changed += 1

    config = apt_root / "apt.conf.d/99zz-appflowy-network"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(NETWORK_CONFIG + (ARCHIVE_CONFIG if keep_archives else ""))
    return changed


def restore_archives(seed: Path, destination: Path) -> int:
    """Reuse packages whose name, size and SHA-256 match this runner's APT index."""
    packages = set()
    for archive in seed.glob("*.deb"):
        # APT package names have no underscores. Versions can contain an encoded epoch.
        parts = re.fullmatch(r"([a-z0-9][a-z0-9+.-]*)_[^/\s_]+_([a-z0-9-]+)\.deb", archive.name)
        if parts and archive.is_file() and not archive.is_symlink():
            packages.add(f"{parts[1]}:{parts[2]}")
    if not packages:
        return 0

    # --print-uris reads the authenticated local index without downloading or
    # installing anything. A runner image change can make the seed inapplicable.
    index = subprocess.run(
        ["apt-get", "-qq", "--print-uris", "-o", "Acquire::ForceHash=SHA256",
         "download", *sorted(packages)], text=True, capture_output=True, check=False,
    )
    if index.returncode:
        print("APT could not match the preparation package set. Using repository downloads.")
        return 0

    restored = 0
    for line in index.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        _, name, size, checksum = fields
        if (Path(name).name != name or not name.endswith(".deb") or not size.isdecimal()
                or not re.fullmatch(r"SHA256:[0-9a-f]{64}", checksum)):
            continue
        source = seed / name
        if source.is_symlink() or not source.is_file() or source.stat().st_size != int(size):
            continue
        # APT can reuse a file already in its archive by size. Publish only the
        # bytes we hashed, even if the incoming file changes during the copy.
        digest = hashlib.sha256()
        with source.open("rb") as incoming, NamedTemporaryFile(dir=destination, delete=False) as staged:
            temporary = Path(staged.name)
            try:
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    staged.write(chunk)
                if digest.hexdigest() == checksum.removeprefix("SHA256:"):
                    staged.flush()
                    temporary.chmod(0o644)
                    temporary.replace(destination / name)
                    restored += 1
            finally:
                temporary.unlink(missing_ok=True)
    return restored


if __name__ == "__main__":
    # This script owns only an ephemeral CI runner's settings. Local tests call
    # configure_apt with a temporary directory instead of modifying /etc/apt.
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        raise SystemExit("APT configuration is only for GitHub Linux runners")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--network-only", action="store_true", help="Use the runner's existing package cache")
    mode.add_argument("--restore", type=Path, metavar="DIRECTORY")
    args = parser.parse_args()
    if not args.network_only:
        ARCHIVES.mkdir(mode=0o755, parents=True, exist_ok=True)
    if args.restore is None:
        changed = configure_apt(Path("/etc/apt"), keep_archives=not args.network_only)
        print(f"Configured APT downloads and updated {changed} Ubuntu source files")
    else:
        print(f"Reused {restore_archives(args.restore, ARCHIVES)} verified Ubuntu packages")
