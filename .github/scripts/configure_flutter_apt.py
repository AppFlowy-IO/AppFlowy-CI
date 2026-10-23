"""Configure package downloads on disposable GitHub-hosted Ubuntu runners."""

import os
from pathlib import Path
import re


AZURE_ARCHIVE = re.compile(
    r"https?://azure\.archive\.ubuntu\.com(?=/ubuntu(?:/|\s|$))"
)
NETWORK_CONFIG = """// Avoid spending the Flutter test budget retrying package downloads.
Acquire::Retries "1";
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
"""


def configure_apt(apt_root: Path) -> int:
    # Keep suites, components, signatures and third-party repositories intact.
    # The Azure mirror stalled for 42–59 minutes in run 39783354102; use
    # Ubuntu's primary archive instead of retrying that mirror for every .deb.
    # GitHub runner sources use mirror+file:/etc/apt/apt-mirrors.txt. Rewriting
    # only .list/.sources files leaves its preferred Azure mirror unchanged.
    sources = [apt_root / "sources.list", apt_root / "apt-mirrors.txt"]
    for extension in ("*.list", "*.sources"):
        sources.extend(sorted((apt_root / "sources.list.d").glob(extension)))

    changed = 0
    for source in sources:
        if not source.is_file():
            continue
        original = source.read_text()
        updated = AZURE_ARCHIVE.sub("https://archive.ubuntu.com", original)
        if updated != original:
            source.write_text(updated)
            changed += 1

    config = apt_root / "apt.conf.d/99zz-appflowy-network"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(NETWORK_CONFIG)
    return changed


if __name__ == "__main__":
    # This script owns only an ephemeral CI runner's settings. Local tests call
    # configure_apt with a temporary directory instead of modifying /etc/apt.
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        raise SystemExit("Flutter APT configuration is only for GitHub Linux runners")
    changed = configure_apt(Path("/etc/apt"))
    print(f"Configured bounded APT downloads; switched {changed} source files to Ubuntu's primary archive")
