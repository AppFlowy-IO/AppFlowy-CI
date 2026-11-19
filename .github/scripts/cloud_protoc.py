"""Select the protobuf compiler used by the Cloud integration build."""

from pathlib import Path
import shutil
import subprocess
import tomllib


def vendors_protoc(crate):
    manifest = crate / "Cargo.toml"
    build = crate / "build.rs"
    if not (manifest.is_file() and build.is_file()):
        return False
    dependencies = tomllib.loads(manifest.read_text()).get("build-dependencies", {})
    return ("protoc-bin-vendored" in dependencies
            and "protoc_bin_vendored::protoc_bin_path" in build.read_text())


def prepare(source):
    # Older Server revisions relied on the system compiler. Reading their manifests and
    # build scripts leaves the choice of compiler with the source under test.
    if all(vendors_protoc(source / "libs" / owner) for owner in ("appflowy-proto", "collab")):
        print("Both Cloud protobuf generators use their locked Cargo compiler.")
        return
    compiler = shutil.which("protoc")
    if compiler and subprocess.run([compiler, "--version"]).returncode == 0:
        return
    subprocess.run(["sudo", "apt-get", "-o", "Acquire::Retries=2", "update"], check=True)
    subprocess.run(["sudo", "apt-get", "-o", "Acquire::Retries=2", "install", "-y",
                    "--no-install-recommends", "protobuf-compiler"], check=True)
    subprocess.run(["protoc", "--version"], check=True)


if __name__ == "__main__":
    prepare(Path.cwd())
