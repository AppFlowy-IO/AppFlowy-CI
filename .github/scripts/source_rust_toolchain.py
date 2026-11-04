#!/usr/bin/env python3
"""Resolve the compiler from the same immutable Cloud checkout used by the images and tests."""

import argparse
from pathlib import Path
import re
import tomllib


def source_toolchain(source, fallback):
    """Use the source pin; retain the caller fallback only for older unpinned sources."""
    path = Path(source) / "rust-toolchain.toml"
    version = tomllib.loads(path.read_text())["toolchain"]["channel"] if path.exists() else fallback
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Cloud tests require an exact Rust version such as 1.98.1")
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--fallback", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    version = source_toolchain(args.source, args.fallback)
    with args.github_output.open("a") as output:
        print(f"rust_toolchain={version}", file=output)
    print(f"Cloud test Rust toolchain: {version}")


if __name__ == "__main__":
    main()
