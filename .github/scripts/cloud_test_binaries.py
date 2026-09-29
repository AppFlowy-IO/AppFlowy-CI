#!/usr/bin/env python3
"""Build Cloud's root test executables once and run their unchanged libtest harnesses.

The archive carries executables and runtime assets, not Cargo's intermediate build cache.
Build and test jobs must use the same revision and checkout path, targeting the test runner's ABI:
Rust's env!("CARGO_MANIFEST_DIR") and env!("CARGO_BIN_EXE_...") embed absolute paths.
"""

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile


MANIFEST = Path("target/cloud-test-binaries.json")
RUNTIME = Path("target/cloud-test-runtime")
CROSS_TARGET = "x86_64-unknown-linux-gnu"
AMD64_RUNNER = ["Linux", "x86_64", "ubuntu", "24.04"]


def command_output(*command):
    return subprocess.check_output(command, text=True).strip()


def host_identity():
    release = platform.freedesktop_os_release()
    return [platform.system(), platform.machine(), release["ID"], release["VERSION_ID"]]


def features(value):
    return sorted(set(value.replace(",", " ").split()))


def target_path(path, workspace):
    """Restrict both archive members and execution paths to this checkout's target directory."""
    path = Path(path)
    if not path.is_absolute():
        path = workspace / path
    relative = path.relative_to(workspace)
    if not relative.parts or relative.parts[0] != "target" or ".." in relative.parts:
        raise ValueError(f"Build artifact is outside target/: {path}")
    target = (workspace / "target").resolve()
    if not target.is_relative_to(workspace) or not path.resolve().is_relative_to(target):
        raise ValueError(f"Build artifact escapes target/: {path}")
    return relative


def source_revision(value=None):
    value = value or command_output("git", "rev-parse", "HEAD")
    if not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("Expected an immutable 40-character Cloud commit")
    return value


def verify_amd64_executable(path):
    with path.open("rb") as executable:
        header = executable.read(20)
    # ELF64, little endian, e_machine=EM_X86_64. Catch accidentally archived host binaries.
    if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
        raise ValueError(f"Expected an AMD64 Linux executable: {path}")


def build(archive, feature_names, target=None, source_sha=None):
    workspace = Path.cwd().resolve()
    source_sha = source_revision(source_sha)
    if target not in (None, CROSS_TARGET):
        raise ValueError(f"Unsupported Cloud test target: {target}")
    target_args = ["--target", target] if target else []
    build_dir = workspace / "target" / (target or "") / "debug"
    metadata = json.loads(command_output("cargo", "metadata", "--locked", "--no-deps",
                                         "--format-version=1"))
    if Path(metadata["target_directory"]) != workspace / "target":
        raise ValueError("Shared Cloud tests require the default workspace target/ directory")
    package = next(pkg for pkg in metadata["packages"]
                   if Path(pkg["manifest_path"]) == workspace / "Cargo.toml")
    members = set(metadata["workspace_members"])
    result = subprocess.run(
        ["cargo", "test", "--locked", "--no-run", "--features", ",".join(feature_names),
         "--message-format=json-render-diagnostics", *target_args],
        stdout=subprocess.PIPE, text=True, check=True,
    )
    messages = []
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        if isinstance(message, dict):
            messages.append(message)
    if not any(msg.get("reason") == "build-finished" and msg.get("success") for msg in messages):
        raise ValueError("Cargo did not report a successful test build")

    files = {}
    tests = {}
    library_paths = {str(target_path(path, workspace)) for path in (build_dir, build_dir / "deps")}
    runtime_env = {
        "CARGO_MANIFEST_DIR": str(workspace),
        "CARGO_MANIFEST_PATH": str(workspace / "Cargo.toml"),
        "CARGO_PKG_NAME": package["name"],
        "CARGO_PKG_VERSION": package["version"],
    }

    def add_file(path):
        relative = target_path(path, workspace)
        files[str(relative)] = workspace / relative
        return str(relative)

    for message in messages:
        reason = message.get("reason")
        if reason == "compiler-artifact" and message["package_id"] == package["id"]:
            executable = message.get("executable")
            if not executable:
                continue
            if target:
                verify_amd64_executable(Path(executable))
            test_target = message["target"]
            path = add_file(executable)
            if message["profile"]["test"] or "test" in test_target["kind"]:
                tests[path] = {"name": test_target["name"], "kind": test_target["kind"], "path": path}
            elif "bin" in test_target["kind"]:
                runtime_env[f'CARGO_BIN_EXE_{test_target["name"]}'] = executable
        elif reason == "build-script-executed":
            for linked in message.get("linked_paths", []):
                path = Path(linked.split("=", 1)[-1])
                if path.is_relative_to(build_dir):
                    library_paths.add(str(target_path(path, workspace)))
            # Workspace build scripts can generate fixtures referenced through OUT_DIR.
            # External native builds' .o/.a files are already linked into the executables.
            out_dir = Path(message["out_dir"])
            if message["package_id"] in members and out_dir.is_relative_to(build_dir):
                for path in out_dir.rglob("*"):
                    if path.is_file():
                        add_file(path)
                if message["package_id"] == package["id"]:
                    runtime_env["OUT_DIR"] = str(out_dir)
                    runtime_env.update(message.get("env", []))

    if not tests or not any(test["kind"] == ["test"] and test["name"] == "main"
                            for test in tests.values()):
        raise ValueError("Cloud's main integration test executable is missing from the build")
    # Cargo adds these dynamic-library paths when launching test executables. Include
    # generated shared libraries and the Rust runtime, while leaving static intermediates out.
    for path in build_dir.rglob("*.so*"):
        if path.is_file():
            add_file(path)
    runtime_dir = workspace / RUNTIME
    runtime_dir.mkdir(parents=True, exist_ok=True)
    for path in Path(command_output("rustc", "--print", "target-libdir", *target_args)).glob("*.so*"):
        destination = runtime_dir / path.name
        shutil.copy2(path, destination)
        add_file(destination)
    library_paths.add(str(RUNTIME))
    manifest = {
        "version": 1,
        "source_sha": source_sha,
        "workspace": str(workspace),
        # Cross-built archives must pass `verify` inside this AMD64 Ubuntu runtime
        # before upload. Compilers and build scripts still execute natively on ARM64.
        "host": AMD64_RUNNER if target else host_identity(),
        "features": feature_names,
        "tests": sorted(tests.values(), key=lambda test: (
            {"lib": 0, "bin": 1, "test": 2}.get(test["kind"][0], 3), test["name"])),
        "library_paths": sorted(library_paths),
        "env": runtime_env,
    }
    content = json.dumps(manifest, indent=2).encode()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz", compresslevel=1, dereference=True) as bundle:
        info = tarfile.TarInfo(str(MANIFEST))
        info.size = len(content)
        info.mode = 0o644
        bundle.addfile(info, io.BytesIO(content))
        for relative, path in sorted(files.items()):
            bundle.add(path, arcname=relative, recursive=False)
    print(f"Archived {len(tests)} test executables: {archive} ({archive.stat().st_size:,} bytes)")


def validate_manifest(manifest, workspace, source_sha=None):
    if manifest["version"] != 1:
        raise ValueError("Unsupported Cloud test archive version")
    if manifest["source_sha"] != source_revision(source_sha):
        raise ValueError("Test archive and source checkout have different commits")
    if manifest["workspace"] != str(workspace):
        raise ValueError("Test archive and source checkout must have identical absolute paths")
    if manifest["host"] != host_identity():
        raise ValueError("Test archive and runner must use the same OS release and architecture")
    if not manifest["tests"]:
        raise ValueError("Test archive contains no test executables")
    for test in manifest["tests"]:
        target_path(test["path"], workspace)
    for path in manifest["library_paths"]:
        target_path(path, workspace)


def restore(archive, source_sha=None):
    workspace = Path.cwd().resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != "target" or not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe member in test archive: {member.name}")
            if member.name in names:
                raise ValueError(f"Duplicate member in test archive: {member.name}")
            names.add(member.name)
            target_path(member.name, workspace)
        manifest = json.load(bundle.extractfile(str(MANIFEST)))
        validate_manifest(manifest, workspace, source_sha)
        if any(test["path"] not in names for test in manifest["tests"]):
            raise ValueError("Test archive is missing an executable listed in its manifest")
        bundle.extractall(workspace, filter="data")
    for test in manifest["tests"]:
        if not os.access(workspace / test["path"], os.X_OK):
            raise ValueError(f'Test executable is not executable: {test["path"]}')
    message = (f'Restored {len(manifest["tests"])} precompiled Cloud test executables for '
               f'{manifest["source_sha"]}. This runner will not compile root Cloud tests.')
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(f"### Shared Cloud test binaries\n\n{message}\n")
    return manifest


def runtime_environment(manifest, workspace):
    environment = {**os.environ, **manifest["env"]}
    paths = [str(workspace / path) for path in manifest["library_paths"]]
    if environment.get("LD_LIBRARY_PATH"):
        paths.append(environment["LD_LIBRARY_PATH"])
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(paths)
    return environment


def verify(archive, source_sha):
    """Check the target loader and libtest startup; service-backed tests run on GitHub."""
    workspace = Path.cwd().resolve()
    if archive:
        manifest = restore(archive, source_sha)
    else:
        manifest = json.loads((workspace / MANIFEST).read_text())
        validate_manifest(manifest, workspace, source_sha)
    environment = runtime_environment(manifest, workspace)
    for test in manifest["tests"]:
        subprocess.run([str(workspace / test["path"]), "--list"], cwd=workspace,
                       env=environment, stdout=subprocess.PIPE, check=True, timeout=120)
    print(f'Verified startup of {len(manifest["tests"])} test executables on {host_identity()}')


def run(arguments):
    """Accept the existing root cargo-test invocations, rejecting flags requiring a rebuild."""
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    cargo_args, test_args = arguments[:separator], arguments[separator + 1:]
    parser = argparse.ArgumentParser(description="Run precompiled root Cloud tests")
    parser.add_argument("command", choices=["test"])
    parser.add_argument("filter", nargs="?")
    parser.add_argument("--locked", action="store_true")
    parser.add_argument("--features", action="append", default=[])
    parser.add_argument("--lib", action="store_true")
    parser.add_argument("--bins", action="store_true")
    parser.add_argument("--test", action="append", default=[])
    args = parser.parse_intermixed_args(cargo_args)
    workspace = Path.cwd().resolve()
    manifest = json.loads((workspace / MANIFEST).read_text())
    validate_manifest(manifest, workspace)
    if features(" ".join(args.features)) != manifest["features"]:
        raise ValueError("Requested Cargo features differ from the precompiled test archive")
    selected = [test for test in manifest["tests"] if (
        not (args.lib or args.bins or args.test)
        or args.lib and "lib" in test["kind"]
        or args.bins and "bin" in test["kind"]
        or "test" in test["kind"] and test["name"] in args.test
    )]
    if not selected or any(name not in {test["name"] for test in selected
                                       if "test" in test["kind"]} for name in args.test):
        raise ValueError("Requested test target is missing from the precompiled archive")
    for requested, kind in ((args.lib, "lib"), (args.bins, "bin")):
        if requested and not any(kind in test["kind"] for test in selected):
            raise ValueError(f"Requested {kind} test target is missing from the archive")
    environment = runtime_environment(manifest, workspace)
    for test in selected:
        print(f'Running precompiled {test["name"]} ({test["path"]})', flush=True)
        command = [str(workspace / test["path"])]
        if args.filter:
            command.append(args.filter)
        result = subprocess.run(command + test_args, cwd=workspace, env=environment)
        if result.returncode:
            return result.returncode if result.returncode > 0 else 128 - result.returncode
    return 0


def main():
    # Keep Cargo's arguments intact: final snapshot cleanup invokes the adapter as `cargo test`.
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        return run(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--archive", type=Path, required=True)
    builder.add_argument("--features", required=True)
    builder.add_argument("--target", choices=[CROSS_TARGET])
    builder.add_argument("--source-sha", help="Verified source revision for a Docker context without .git")
    restorer = commands.add_parser("restore")
    restorer.add_argument("--archive", type=Path, required=True)
    verifier = commands.add_parser("verify")
    verifier.add_argument("--archive", type=Path, help="Restore first; omit for an extracted archive")
    verifier.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    if args.command == "build":
        build(args.archive, features(args.features), args.target, args.source_sha)
    elif args.command == "verify":
        verify(args.archive, args.source_sha)
    else:
        restore(args.archive)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"Cloud test binaries: {error}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
