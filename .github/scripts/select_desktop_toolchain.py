#!/usr/bin/env python3
"""Honor the tested Desktop's SDK pins before workflow actions install tools."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tomllib


def pinned_version(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError(f"{name} must specify an exact release version")
    return value


def select(frontend: Path, fallback_rust: str) -> dict[str, str]:
    rust_pin = frontend / "rust-toolchain.toml"
    rust = tomllib.loads(rust_pin.read_text())["toolchain"]["channel"] if rust_pin.exists() else fallback_rust
    result = {"RUST_TOOLCHAIN": pinned_version(rust, "Rust toolchain")}
    # Desktop currently has no Flutter pin; retain the workflow default then.
    # Respect explicit source pins if the application adopts one later.
    for root in (frontend / "appflowy_flutter", frontend, frontend.parent):
        fvm = root / ".fvmrc"
        flutter = root / ".flutter-version"
        tools = root / ".tool-versions"
        if fvm.exists():
            result["FLUTTER_VERSION"] = pinned_version(json.loads(fvm.read_text())["flutter"], "Flutter")
            break
        if flutter.exists():
            result["FLUTTER_VERSION"] = pinned_version(flutter.read_text().strip(), "Flutter")
            break
        if tools.exists():
            versions = re.findall(r"^flutter\s+(\S+)\s*$", tools.read_text(), re.MULTILINE)
            if versions:
                if len(versions) != 1:
                    raise ValueError("Source has conflicting Flutter pins")
                result["FLUTTER_VERSION"] = pinned_version(versions[0], "Flutter")
                break
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Desktop frontend directory")
    parser.add_argument("--fallback-rust", required=True)
    args = parser.parse_args()
    try:
        values = select(args.source, args.fallback_rust)
        with open(os.environ["GITHUB_ENV"], "a") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
                print(f"Selected {name}={value} from tested Desktop source/fallback")
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Desktop toolchain selection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
