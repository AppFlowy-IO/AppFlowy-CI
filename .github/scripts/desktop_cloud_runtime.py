#!/usr/bin/env python3
"""Run Desktop against one verified Cloud image set without publishing tags.

The optional pair is deliberately all-or-nothing. Once supplied, every failed
provenance check stops the job; a registry image is never a recovery fallback.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import tomllib
from urllib.parse import parse_qs, urlsplit


REPOSITORY = "AppFlowy-IO/AppFlowy-Cloud-Premium"
WORKFLOW = ".github/workflows/build_ci_images_self_hosted.yml"
RELEASE_WORKFLOW = ".github/workflows/build_docker_self_hosted.yml"
SERVICES = ("appflowy_cloud", "appflowy_worker", "appflowy_search", "appflowy_mcp")
SERVER_URL = "https://github.com/appflowy-io/appflowy-cloud-premium"
CLIENT_URL = "https://github.com/appflowy-io/appflowy-client"


def command(arguments, *, cwd=None, env=None, capture=True):
    # Captured Compose configuration contains the job's credentials. Never echo
    # it, even if a subsequent validation fails.
    if capture:
        return subprocess.check_output(arguments, cwd=cwd, env=env, text=True, timeout=60).strip()
    subprocess.run(arguments, cwd=cwd, env=env, check=True)
    return ""


def validate_pair(source_sha, image_run_id):
    if not source_sha and not image_run_id:
        return False
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha or ""):
        raise ValueError("Exact Cloud mode requires an immutable 40-character source SHA")
    if not re.fullmatch(r"[1-9][0-9]*", image_run_id or ""):
        raise ValueError("Exact Cloud mode requires its private image build run ID")
    return True


def desktop_pin(desktop, source_sha, *, allow_equivalent_shared_source=False):
    with (desktop / "frontend/rust-lib/Cargo.toml").open("rb") as handle:
        manifest = tomllib.load(handle)
    dependencies = manifest["workspace"]["dependencies"]
    pins = {name: dependencies[name] for name in ("collab", "appflowy-entity", "appflowy-proto")}
    pins["client-api"] = manifest["patch"]["crates-io"]["client-api"]
    sources = set()
    for name, pin in pins.items():
        if (not isinstance(pin, dict) or "path" in pin
                or not re.fullmatch(r"[a-f0-9]{40}", pin.get("rev", ""))):
            raise ValueError(f"Desktop {name} must use an immutable shared revision")
        sources.add((pin.get("git", "").removesuffix(".git").lower(), pin["rev"]))
    if len(sources) != 1:
        raise ValueError("Desktop shared pins must use the same repository and revision")
    repository, revision = sources.pop()
    if repository not in (SERVER_URL, CLIENT_URL) or (
        repository == SERVER_URL and revision != source_sha and not allow_equivalent_shared_source
    ):
        raise ValueError("Desktop pins do not identify the requested immutable Cloud revision")
    with (desktop / "frontend/rust-lib/Cargo.lock").open("rb") as handle:
        packages = tomllib.load(handle)["package"]
    locked = set()
    for package in packages:
        source = package.get("source", "")
        if not source.startswith("git+"):
            continue
        url = urlsplit(source[4:])
        origin = f"{url.scheme}://{url.netloc}{url.path}".removesuffix(".git").lower()
        if origin not in (SERVER_URL, CLIENT_URL):
            continue
        if (origin != repository or url.fragment != revision
                or parse_qs(url.query) != {"rev": [revision]}):
            raise ValueError("Desktop lockfile has a different shared source")
        locked.add(package["name"])
    if not set(pins).issubset(locked):
        raise ValueError("Desktop lockfile is missing a pinned shared package")
    return {"shared_source_kind": "generated_client" if repository == CLIENT_URL else "server",
            "shared_source_sha": revision}


def source_inventory(directory):
    result = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Generated source must not contain symlinks")
        relative = path.relative_to(directory).as_posix()
        if path.is_file() and relative != "Cargo.lock":
            result[relative] = [hashlib.sha256(path.read_bytes()).hexdigest(),
                                bool(path.stat().st_mode & 0o111)]
    return result


def equivalent_server_source(cloud, source_sha, shared_cloud, shared_sha, run=command):
    """Compare complete server-owned exports; never guess shared paths in CI."""
    if (shared_cloud is None or run(["git", "rev-parse", "HEAD"], cwd=shared_cloud) != shared_sha
            or run(["git", "remote", "get-url", "origin"], cwd=shared_cloud).removesuffix(".git").lower() != SERVER_URL):
        raise ValueError("Shared Cloud checkout differs from the Desktop pin")
    with tempfile.TemporaryDirectory(prefix="desktop-equivalent-cloud-") as temporary:
        root = Path(temporary)
        exports = []
        recipes = []
        for name, repository, revision in (("runtime", cloud, source_sha), ("shared", shared_cloud, shared_sha)):
            archive = root / f"{name}.tar"
            run(["git", "archive", "--format=tar", f"--output={archive}", revision], cwd=repository)
            with tarfile.open(archive) as contents:
                if any(not (member.isfile() or member.isdir()) for member in contents.getmembers()):
                    raise ValueError("Shared source archive must contain only regular files and directories")
                contents.extractall(root / name, filter="data")
            script = root / name / "scripts/sync_client_repo.sh"
            recipes.append(hashlib.sha256(script.read_bytes()).hexdigest())
            destination = root / f"{name}-client"
            destination.mkdir()
            run(["bash", str(script), str(root / name), str(destination)])
            with (destination / "Cargo.toml").open("rb") as handle:
                members = tomllib.load(handle)["workspace"]["members"]
            required = {f"libs/{crate}" for crate in ("client-api", "collab", "appflowy-entity", "appflowy-proto")}
            if (not required.issubset(members)
                    or any(not (destination / member / "Cargo.toml").is_file() for member in members)):
                raise ValueError("Generated shared workspace is incomplete")
            exports.append(source_inventory(destination))
        if recipes[0] != recipes[1] or not exports[0] or exports[0] != exports[1]:
            raise ValueError("Pinned shared source differs from the requested Cloud runtime")
        return {"equivalent_shared_source_sha256": hashlib.sha256(json.dumps(exports[0], sort_keys=True).encode()).hexdigest(),
                "sync_script_sha256": recipes[0]}


def canonicalize_generated_protos(expected, actual, cloud, run=command):
    """The mirror's Cargo check regenerates prost output before committing it.

    Only schema-backed generated modules may differ in formatting. Keep schemas,
    authored Rust, file sets and executable modes under the ordinary strict check.
    """
    before, after = source_inventory(expected), source_inventory(actual)
    candidates = []
    for name in before.keys() & after.keys():
        path = Path(name)
        if (before[name] == after[name] or before[name][1] != after[name][1]
                or path.parent.as_posix() != "libs/appflowy-proto/src/pb"
                or path.suffix != ".rs"):
            continue
        schema = Path("libs/appflowy-proto/proto") / (path.stem + ".proto")
        if (schema.as_posix() not in before or before[schema.as_posix()] != after.get(schema.as_posix())
                or not re.search(r"(?m)^package " + re.escape(path.stem) + r";\s*$",
                                 (expected / schema).read_text())
                or any(not (root / name).read_bytes().startswith(b"// This file is @generated by prost-build.\n")
                       for root in (expected, actual))):
            continue
        candidates.append(name)
    if not candidates:
        return {}
    with (cloud / "rust-toolchain.toml").open("rb") as handle:
        toolchain = tomllib.load(handle)["toolchain"]["channel"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", toolchain):
        raise ValueError("Generated protobuf verification requires the source's pinned Rust toolchain")
    config = cloud / "rustfmt.toml"
    if not config.is_file():
        raise ValueError("Generated protobuf verification requires the source's rustfmt configuration")
    try:
        version = run(["rustup", "run", toolchain, "rustc", "--version"])
        if not version.startswith(f"rustc {toolchain} "):
            raise ValueError("Generated protobuf verification resolved a different Rust toolchain")
        run(["rustup", "run", toolchain, "rustfmt", "--version"])
        for root, original in ((expected, before), (actual, after)):
            run(["rustup", "run", toolchain, "rustfmt", "--config-path", str(config),
                 *[str(root / name) for name in sorted(candidates)]])
            current = source_inventory(root)
            if ({name: value for name, value in current.items() if name not in candidates}
                    != {name: value for name, value in original.items() if name not in candidates}):
                raise ValueError("Generated protobuf formatting changed source outside the allowed modules")
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Install rustfmt for source-pinned Rust {toolchain} before Client verification") from error
    return {"generated_proto_format_toolchain": toolchain,
            "generated_proto_format_paths": sorted(candidates),
            "generated_proto_format_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()}


def generated_client_source(cloud, source_sha, client, client_sha, run=command):
    if (client is None or run(["git", "rev-parse", "HEAD"], cwd=client) != client_sha
            or run(["git", "remote", "get-url", "origin"], cwd=client).removesuffix(".git").lower() != CLIENT_URL):
        raise ValueError("Generated Client checkout differs from its canonical pinned revision")
    # Generate from committed bytes: the live Cloud checkout has job-owned Compose edits.
    # No Cargo resolution or services are needed, and the existing sync script owns the recipe.
    with tempfile.TemporaryDirectory(prefix="desktop-generated-client-") as temporary:
        root = Path(temporary)
        for name, repository, revision in (("cloud", cloud, source_sha), ("client", client, client_sha)):
            archive = root / f"{name}.tar"
            run(["git", "archive", "--format=tar", f"--output={archive}", revision], cwd=repository)
            with tarfile.open(archive) as contents:
                if any(not (member.isfile() or member.isdir()) for member in contents.getmembers()):
                    raise ValueError("Shared source archive must contain only regular files and directories")
                contents.extractall(root / name, filter="data")
        expected = root / "expected"
        expected.mkdir()
        script = root / "cloud/scripts/sync_client_repo.sh"
        run(["bash", str(script), str(root / "cloud"), str(expected)])

        formatting = canonicalize_generated_protos(expected, root / "client", root / "cloud", run)
        generated = source_inventory(expected)
        # The standalone check writes Client's root lock; downstream builds use Desktop's
        # checked lock instead. Every other tracked Client file must be generated identically.
        if not generated or generated != source_inventory(root / "client"):
            raise ValueError("Generated Client source differs from the requested Cloud source")
        return {**formatting, "client_source_sha": client_sha,
                "generated_source_sha256": hashlib.sha256(json.dumps(generated, sort_keys=True).encode()).hexdigest(),
                "sync_script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}


def verify_build(build, source_sha):
    if (build.get("repository", {}).get("full_name") != REPOSITORY
            or build.get("status") != "completed"
            or build.get("conclusion") != "success"):
        raise ValueError("Expected a successful private integration-image or release workflow")
    manual_main = build.get("head_branch") == "main" and build.get("event") == "workflow_dispatch"
    if build.get("path") == RELEASE_WORKFLOW:
        release_tag = (build.get("event") == "push" and build.get("head_sha") == source_sha
                       and re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9_.-]*", build.get("head_branch", "")))
        if (not (manual_main or release_tag)
                or not re.fullmatch(r"Self-hosted Docker publish [A-Za-z0-9_.-]+ from .+ \(linux/(?:arm64,linux/)?amd64\)",
                                    build.get("display_title", ""))):
            raise ValueError("Release image build must publish Linux AMD64 images")
        return "release_registry"
    title = rf"CI images [1-9][0-9]*-[1-9][0-9]* \({source_sha}\)(?: \[PR #[1-9][0-9]*\])?"
    if not manual_main or build.get("path") != WORKFLOW or not re.fullmatch(title, build.get("display_title", "")):
        raise ValueError("Private image build does not identify the requested Cloud revision")
    return "ci_archive"


def release_image_proof(log, service, source_sha):
    # The release workflow records these in a single final step environment. Job
    # logs are tied to the requested successful attempt, not mutable registry tags.
    # GitHub masks the Docker Hub username, even though it is the canonical namespace.
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log)
    clean = re.sub(r"(?m)^\d{4}-\d\d-\d\dT[0-9:.]+Z ", "", clean)
    proofs = []
    for group in clean.split("##[group]")[1:]:
        group = group.split("##[endgroup]", 1)[0]
        if not group.startswith("Run {\n") or "\nenv:\n" not in group:
            continue
        pairs = re.findall(r"(?m)^  (SOURCE_SHA|IMAGE|IMAGE_NAME|PLATFORMS|PUBLISHED|DIGEST): (.*)$",
                           group.split("\nenv:\n", 1)[1])
        values = dict(pairs)
        if "SOURCE_SHA" not in values:
            continue
        image = f"appflowyinc/{service}_premium"
        shown = values.get("IMAGE_NAME", "")
        if (len(pairs) != 6 or len(values) != 6 or values["SOURCE_SHA"] != source_sha
                or shown not in (image, f"***/{service}_premium")
                or not re.fullmatch(re.escape(shown) + r":[A-Za-z0-9_][A-Za-z0-9_.-]*", values["IMAGE"])
                or values["PUBLISHED"] != "true"
                or values["PLATFORMS"] not in ("linux/amd64", "linux/arm64,linux/amd64")
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", values["DIGEST"])):
            raise ValueError(f"Release result does not prove the requested source/image for {service}")
        proofs.append({"reference": image + "@" + values["DIGEST"],
                       "published_tag": image + ":" + values["IMAGE"].split(":", 1)[1]})
    if len(proofs) != 1:
        raise ValueError(f"Expected exactly one release result for {service}")
    return proofs[0]


def release_images(image_run_id, build, source_sha, run=command):
    jobs = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{image_run_id}/attempts/"
                                      f"{build['run_attempt']}/jobs?per_page=100"]))
    if jobs.get("total_count", len(jobs["jobs"])) > 100:
        raise ValueError("Unexpectedly large release job set")
    # Recent gh versions reject ANSI bytes even when stdout is captured. Only
    # log reads opt in: the parser consumes these bytes without printing them.
    # Probe once so older runner/local CLIs remain supported without retries.
    log_options = ["--allow-escape-sequences"] if "--allow-escape-sequences" in run(["gh", "api", "--help"]) else []
    images = {}
    selected = (*SERVICES, "gotrue") if any(job["name"] == "Build and publish gotrue" for job in jobs["jobs"]) else SERVICES
    for service in selected:
        matching = [job for job in jobs["jobs"] if job["name"] == f"Build and publish {service}"]
        if (len(matching) != 1 or matching[0].get("status") != "completed"
                or matching[0].get("conclusion") != "success"
                or not any(step["name"] == "Record the result" and step["conclusion"] == "success"
                           for step in matching[0].get("steps", []))):
            raise ValueError(f"Release build is missing the successful {service} image")
        job = matching[0]
        proof = release_image_proof(run(["gh", "api", *log_options, f"repos/{REPOSITORY}/actions/jobs/{job['id']}/logs"]),
                                    service, source_sha)
        images[service] = {**proof, "job_id": job["id"]}
    if len({proof["published_tag"].split(":", 1)[1] for proof in images.values()}) != 1:
        raise ValueError("Release services do not share one published version")
    return images


def prepare_request(source_sha, image_run_id, desktop, cloud, desktop_ref, manifest, run=command, *,
                    client=None, shared_cloud=None, allow_equivalent_shared_source=False):
    if not validate_pair(source_sha, image_run_id):
        return False
    if not re.fullmatch(r"[a-f0-9]{40}", desktop_ref or ""):
        raise ValueError("Exact Cloud mode requires an immutable Desktop pr_ref")
    desktop_sha = run(["git", "rev-parse", "HEAD"], cwd=desktop)
    cloud_sha = run(["git", "rev-parse", "HEAD"], cwd=cloud)
    if desktop_sha != desktop_ref or cloud_sha != source_sha:
        raise ValueError("Checked-out Desktop or Cloud revision differs from the request")
    ci_sha = run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2])
    if ci_sha != os.environ["GITHUB_SHA"]:
        raise ValueError("CI runtime tools differ from the workflow revision")
    shared = desktop_pin(desktop, source_sha, allow_equivalent_shared_source=allow_equivalent_shared_source)
    if shared["shared_source_kind"] == "generated_client":
        shared.update(generated_client_source(cloud, source_sha, client, shared["shared_source_sha"], run))
    elif shared["shared_source_sha"] != source_sha:
        shared.update(equivalent_server_source(cloud, source_sha, shared_cloud, shared["shared_source_sha"], run))
    build = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{image_run_id}"]))
    image_kind = verify_build(build, source_sha)
    image_artifacts = []
    published = {}
    if image_kind == "release_registry":
        published = release_images(image_run_id, build, source_sha, run)
    else:
        artifacts = json.loads(run([
            "gh", "api", f"repos/{REPOSITORY}/actions/runs/{image_run_id}/artifacts?per_page=100",
        ]))
        image_artifacts = [item for item in artifacts["artifacts"]
                           if item["name"].startswith("docker-image-")]
        expected = {f"docker-image-{service.removeprefix('appflowy_')}" for service in SERVICES}
        if (len(image_artifacts) != len(expected)
                or {item["name"] for item in image_artifacts} != expected
                or any(item["expired"] for item in image_artifacts)):
            raise ValueError("Private image build must retain exactly one complete, unexpired image set")
    write_manifest(manifest, {
        "desktop_source_sha": desktop_sha,
        "ci_source_sha": ci_sha,
        "cloud_source_sha": source_sha,
        "image_run_id": int(image_run_id),
        "image_run_attempt": build["run_attempt"],
        "image_build_url": build["html_url"],
        "image_workflow_sha": build["head_sha"],
        "image_artifacts": {item["name"]: item["id"] for item in image_artifacts},
        "image_kind": image_kind,
        "release_images": published,
        "stage": "request_verified",
        **shared,
    })
    return True


def write_manifest(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def image_tag(service):
    return f"appflowyinc/{service}_premium:latest-amd64"


def verify_archive(path, service, source_sha):
    # Read metadata directly: never extract arbitrary paths from the tarball.
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if len({member.name for member in members}) != len(members):
            raise ValueError("Duplicate Docker archive paths are not allowed")
        manifest_member = archive.getmember("manifest.json")
        if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
            raise ValueError("Invalid Docker image manifest")
        manifests = json.load(archive.extractfile(manifest_member))
        if (not isinstance(manifests, list) or len(manifests) != 1
                or manifests[0].get("RepoTags") != [image_tag(service)]):
            raise ValueError(f"Unexpected image tag for {service}")
        member = archive.getmember(manifests[0]["Config"])
        if not member.isfile() or member.size > 4 * 1024 * 1024:
            raise ValueError("Invalid Docker image configuration")
        encoded = archive.extractfile(member).read()
        config = json.loads(encoded)
        verify_image(config, source_sha)
        config_id = "sha256:" + hashlib.sha256(encoded).hexdigest()
        proof = {"config_id": config_id}
        if "index.json" in {entry.name for entry in members}:
            # Docker's containerd store identifies an imported image by its OCI
            # manifest; the classic store identifies it by config. Bind both IDs
            # to the same checked config and layers, never accept labels alone.
            index_member = archive.getmember("index.json")
            if not index_member.isfile() or index_member.size > 1024 * 1024:
                raise ValueError("Invalid OCI image index")
            index = json.load(archive.extractfile(index_member))
            descriptors = index.get("manifests", [])
            if (index.get("schemaVersion") != 2
                    or index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
                    or len(descriptors) != 1):
                raise ValueError("Expected one OCI image manifest")
            descriptor = descriptors[0]
            digest = descriptor.get("digest", "")
            annotations = descriptor.get("annotations", {})
            if (descriptor.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                    or descriptor.get("platform") != {"os": "linux", "architecture": "amd64"}
                    or annotations.get("io.containerd.image.name") not in (image_tag(service), "docker.io/" + image_tag(service))
                    or annotations.get("org.opencontainers.image.ref.name") != "latest-amd64"):
                raise ValueError("OCI image identity does not match the archive request")
            manifest_member = archive.getmember("blobs/" + digest.replace(":", "/"))
            if (not manifest_member.isfile() or manifest_member.size > 4 * 1024 * 1024
                    or manifest_member.size != descriptor.get("size")):
                raise ValueError("Invalid OCI image manifest size")
            manifest_bytes = archive.extractfile(manifest_member).read()
            if "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() != digest:
                raise ValueError("OCI image manifest digest differs from its contents")
            manifest = json.loads(manifest_bytes)
            if (manifest.get("schemaVersion") != 2
                    or manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
                    or manifests[0]["Config"] != "blobs/" + config_id.replace(":", "/")
                    or manifest.get("config") != {"mediaType": "application/vnd.oci.image.config.v1+json",
                                                   "digest": config_id, "size": len(encoded)}):
                raise ValueError("OCI image manifest does not reference the verified config")
            layer_paths = []
            for layer in manifest.get("layers", []):
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", layer.get("digest", "")):
                    raise ValueError("Invalid OCI image layer digest")
                layer_path = "blobs/" + layer["digest"].replace(":", "/")
                layer_member = archive.getmember(layer_path)
                if not layer_member.isfile() or layer_member.size != layer.get("size"):
                    raise ValueError("Invalid OCI image layer size")
                layer_paths.append(layer_path)
            if layer_paths != manifests[0].get("Layers"):
                raise ValueError("OCI and Docker manifests describe different layers")
            proof["manifest_id"] = digest
        return proof


def verify_image(config, source_sha):
    # Archive configs and docker inspect use different key casing.
    platform = (config.get("os", config.get("Os")), config.get("architecture", config.get("Architecture")))
    labels = config.get("config", config.get("Config", {})).get("Labels") or {}
    if platform != ("linux", "amd64") or labels.get("org.opencontainers.image.revision") != source_sha:
        raise ValueError("Image platform or source revision does not match the request")


def dependency_services(config, profiles=(), protected_images=None):
    services = config["services"]
    if not set(SERVICES).issubset(services):
        raise ValueError("Compose source does not contain the required Cloud services")
    protected_images = protected_images or {service: image_tag(service) for service in SERVICES}
    for service, reference in protected_images.items():
        if service not in services or services[service].get("image") != reference:
            raise ValueError(f"Compose image differs from the verified artifact for {service}")
    active = set(profiles)
    return sorted(name for name, value in services.items()
                  if name not in protected_images and (not value.get("profiles") or "*" in active
                                              or active.intersection(value["profiles"])))


def start_runtime(cloud, archives, manifest, timeline=False, run=command):
    state = json.loads(manifest.read_text())
    source_sha = state["cloud_source_sha"]
    if run(["git", "rev-parse", "HEAD"], cwd=cloud) != source_sha:
        raise ValueError("Cloud checkout changed after request verification")
    release = state.get("image_kind") == "release_registry"
    references = {service: proof["reference"] for service, proof in state.get("release_images", {}).items()} if release else {
        service: image_tag(service) for service in SERVICES}
    if release and (not set(SERVICES).issubset(references) or set(references) - set((*SERVICES, "gotrue"))
                    or any(not re.fullmatch(re.escape(f"appflowyinc/{service}_premium@sha256:") + r"[a-f0-9]{64}", reference)
                           for service, reference in references.items())):
        raise ValueError("Release manifest must contain one complete immutable image set")
    # Archives are checked as a complete set before Docker mutations. Registry
    # references have already been bound to the selected successful run's jobs.
    archive_proofs = {} if release else {
        service: verify_archive(archives / f"{service}.tar.gz", service, source_sha) for service in SERVICES}
    images = {service: proof["config_id"] for service, proof in archive_proofs.items()}
    env = dict(os.environ)
    for service in references:
        env[f"{service.upper()}_VERSION"] = "latest-amd64"
    if timeline:
        env["APPFLOWY_CLOUD_BUILD_FEATURES"] = "self-host-af,commercial-ci,ci-test"
    compose = ["docker", "compose", "-f", str(cloud / "docker-compose-ci.yml")]
    override = manifest.with_suffix(".compose.json")
    protected = {service: {"pull_policy": "never", **({"image": reference, "platform": "linux/amd64"} if release else {})}
                 for service, reference in references.items()}
    if timeline:
        # The Timeline flavor requires extra compile features; keep its source
        # exact and make that explicit in the provenance instead of mislabelling it.
        protected["appflowy_cloud"].update(image=image_tag("appflowy_cloud"), build={
            "labels": {"org.opencontainers.image.revision": source_sha},
        })
    write_manifest(override, {"services": protected})
    compose += ["-f", str(override)]
    expected_compose = {**references, **({"appflowy_cloud": image_tag("appflowy_cloud")} if timeline else {})}
    config = json.loads(run(compose + ["config", "--format", "json"], cwd=cloud, env=env))
    dependencies = dependency_services(config, env.get("COMPOSE_PROFILES", "").split(","), expected_compose)
    for service, reference in references.items():
        if release:
            run(["docker", "pull", "--platform", "linux/amd64", reference], cwd=cloud, capture=False)
        else:
            run(["docker", "load", "--input", str(archives / f"{service}.tar.gz")], cwd=cloud, capture=False)
        loaded = json.loads(run(["docker", "image", "inspect", reference]))[0]
        verify_image(loaded, source_sha)
        if release:
            if reference not in loaded.get("RepoDigests", []):
                raise ValueError(f"Pulled image digest does not match the release result for {service}")
            images[service] = loaded["Id"]
        elif loaded["Id"] not in archive_proofs[service].values():
            raise ValueError(f"Loaded image differs from verified archive for {service}: "
                             f"loaded={loaded['Id']}, expected={archive_proofs[service]}")
        else:
            images[service] = loaded["Id"]
    if dependencies:
        run(compose + ["pull", *dependencies], cwd=cloud, env=env, capture=False)
    if timeline:
        run(compose + ["build", "appflowy_cloud"], cwd=cloud, env=env, capture=False)
        built = json.loads(run(["docker", "image", "inspect", image_tag("appflowy_cloud")]))[0]
        verify_image(built, source_sha)
        images["appflowy_cloud"] = built["Id"]
    state.update(stage="images_verified", images=images, archive_image_proofs=archive_proofs,
                 timeline_source_build=timeline)
    write_manifest(manifest, state)
    run(compose + ["up", "-d", "--no-build", "--pull", "never"], cwd=cloud, env=env, capture=False)
    # Verify the images actually attached to the containers, not only local tags.
    for service, expected_id in images.items():
        container = run(compose + ["ps", "--all", "--quiet", service], cwd=cloud, env=env)
        if not container or "\n" in container:
            raise ValueError(f"Expected exactly one runtime container for {service}")
        actual = json.loads(run(["docker", "inspect", container]))[0]
        if actual["Image"] != expected_id:
            raise ValueError(f"Runtime container uses a different image for {service}")
    state["stage"] = "runtime_verified"
    write_manifest(manifest, state)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"Desktop `{state['desktop_source_sha']}` tested against Cloud `{source_sha}` "
                         f"from [image build]({state['image_build_url']}) "
                         f"attempt {state['image_run_attempt']}. Timeline source build: {timeline}.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    pins = sub.add_parser("pins")
    pins.add_argument("--cloud-source-sha", default="")
    pins.add_argument("--image-run-id", default="")
    pins.add_argument("--desktop-ref", default="")
    pins.add_argument("--desktop", type=Path, required=True)
    pins.add_argument("--allow-equivalent-shared-source", action="store_true")
    request = sub.add_parser("request")
    request.add_argument("--cloud-source-sha", default="")
    request.add_argument("--image-run-id", default="")
    request.add_argument("--desktop-ref", default="")
    request.add_argument("--desktop", type=Path, required=True)
    request.add_argument("--cloud", type=Path, required=True)
    request.add_argument("--client", type=Path)
    request.add_argument("--shared-cloud", type=Path)
    request.add_argument("--allow-equivalent-shared-source", action="store_true")
    request.add_argument("--manifest", type=Path, required=True)
    start = sub.add_parser("start")
    start.add_argument("--cloud", type=Path, required=True)
    start.add_argument("--archives", type=Path, required=True)
    start.add_argument("--manifest", type=Path, required=True)
    start.add_argument("--timeline", action="store_true")
    args = parser.parse_args()
    if args.operation == "pins":
        revision = ""
        shared_revision = ""
        if validate_pair(args.cloud_source_sha, args.image_run_id):
            if (not re.fullmatch(r"[a-f0-9]{40}", args.desktop_ref)
                    or command(["git", "rev-parse", "HEAD"], cwd=args.desktop) != args.desktop_ref):
                raise ValueError("Exact Cloud mode requires the checked-out immutable Desktop revision")
            shared = desktop_pin(args.desktop, args.cloud_source_sha,
                                 allow_equivalent_shared_source=args.allow_equivalent_shared_source)
            if shared["shared_source_kind"] == "generated_client":
                revision = shared["shared_source_sha"]
            elif shared["shared_source_sha"] != args.cloud_source_sha:
                shared_revision = shared["shared_source_sha"]
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"client-sha={revision}", file=output)
            print(f"shared-cloud-sha={shared_revision}", file=output)
    elif args.operation == "request":
        enabled = prepare_request(args.cloud_source_sha, args.image_run_id, args.desktop,
                                  args.cloud, args.desktop_ref, args.manifest, client=args.client,
                                  shared_cloud=args.shared_cloud,
                                  allow_equivalent_shared_source=args.allow_equivalent_shared_source)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"enabled={str(enabled).lower()}", file=output)
            if enabled:
                state = json.loads(args.manifest.read_text())
                ids = ",".join(str(value) for value in state["image_artifacts"].values())
                print(f"artifact-ids={ids}", file=output)
                print(f"image-kind={state['image_kind']}", file=output)
    else:
        start_runtime(args.cloud.resolve(), args.archives, args.manifest, args.timeline)


if __name__ == "__main__":
    main()
