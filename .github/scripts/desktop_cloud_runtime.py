#!/usr/bin/env python3
"""Run Desktop with published images or an explicitly selected verified image set.

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
SERVICES = ("appflowy_cloud", "appflowy_worker", "appflowy_search", "appflowy_mcp")
KNOWN_IMAGES = (*SERVICES, "appflowy_ai")
GENERATED_PROTOBUF = {
    f"libs/appflowy-proto/src/pb/{name}.rs"
    for name in ("collab", "collab_dependency", "database_blob", "messages", "notification")
}
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


def desktop_pin(desktop, source_sha):
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
    if (repository not in (SERVER_URL, CLIENT_URL)
            or (repository == SERVER_URL and source_sha is not None and revision != source_sha)):
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

        def inventory(directory):
            result = {}
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise ValueError("Generated source must not contain symlinks")
                relative = path.relative_to(directory).as_posix()
                if path.is_file() and relative != "Cargo.lock":
                    result[relative] = [hashlib.sha256(path.read_bytes()).hexdigest(),
                                        bool(path.stat().st_mode & 0o111)]
            return result

        generated = inventory(expected)
        published = inventory(root / "client")
        # Standalone cargo-check regenerates prost files before Client is
        # committed. Canonicalize only those five known generated outputs;
        # source .proto files, build.rs, formatting config and all handwritten
        # bytes and executable modes must already agree exactly.
        if (not generated or generated.keys() != published.keys()
                or any(generated[name][1] != published[name][1]
                       or (name not in GENERATED_PROTOBUF and generated[name] != published[name])
                       for name in generated)):
            raise ValueError("Generated Client source differs from the requested Cloud source")
        protobuf = sorted(GENERATED_PROTOBUF.intersection(generated))
        for directory in (expected, root / "client"):
            for name in protobuf:
                if not (directory / name).read_text().startswith("// This file is @generated by prost-build.\n"):
                    raise ValueError("Known protobuf output is missing its prost generation marker")
            if protobuf:
                run(["rustfmt", "--config-path", str(expected / "rustfmt.toml"),
                     *(str(directory / name) for name in protobuf)])
        generated = inventory(expected)
        # The standalone check writes Client's root lock; downstream builds use Desktop's
        # checked lock instead. Every other tracked Client file must be generated identically.
        if not generated or generated != inventory(root / "client"):
            raise ValueError("Generated Client source differs from the requested Cloud source")
        return {"client_source_sha": client_sha,
                "generated_source_sha256": hashlib.sha256(json.dumps(generated, sort_keys=True).encode()).hexdigest(),
                "normalized_prost_outputs": protobuf,
                "sync_script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}


def verify_build(build, source_sha):
    if (build.get("repository", {}).get("full_name") != REPOSITORY
            or build.get("path") != WORKFLOW
            or build.get("head_branch") != "main"
            or build.get("event") != "workflow_dispatch"
            or build.get("status") != "completed"
            or build.get("conclusion") != "success"):
        raise ValueError("Expected a successful private integration-image workflow")
    title = rf"CI images [1-9][0-9]*-[1-9][0-9]* \({source_sha}\)(?: \[PR #[1-9][0-9]*\])?"
    if not re.fullmatch(title, build.get("display_title", "")):
        raise ValueError("Private image build does not identify the requested Cloud revision")


def runtime_services(profile, image_roles):
    if profile not in ("desktop", "web"):
        raise ValueError("Unknown exact Cloud runtime profile")
    services = {name: name for name in SERVICES if profile == "desktop" or name != "appflowy_mcp"}
    if profile == "web" or "appflowy_ai" in image_roles:
        services["ai"] = "appflowy_ai"
    return services


def ci_source(ci_ref, run):
    if ci_ref and not re.fullmatch(r"[a-f0-9]{40}", ci_ref):
        raise ValueError("CI runtime tools require an immutable 40-character revision")
    revision = run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2])
    if revision != (ci_ref or os.environ["GITHUB_SHA"]):
        raise ValueError("CI runtime tools differ from the workflow revision")
    return revision


def verified_artifacts(response, profile):
    images = [item for item in response["artifacts"] if item["name"].startswith("docker-image-")]
    names = [item["name"] for item in images]
    known = {f"docker-image-{role.removeprefix('appflowy_')}" for role in KNOWN_IMAGES}
    required = {f"docker-image-{role.removeprefix('appflowy_')}"
                for role in runtime_services(profile, ()).values()}
    if (len(names) != len(set(names)) or not required.issubset(names) or not set(names).issubset(known)
            or any(item["expired"] for item in images)):
        raise ValueError("Private image build must retain exactly one complete, unexpired image set")
    return images


def select_prebuilt(desktop, source_sha="", image_run_id="", cloud_version="", *,
                    profile="web", ci_ref="", run=command):
    exact = validate_pair(source_sha, image_run_id)
    if cloud_version:
        if exact:
            raise ValueError("Choose a published Cloud version or an exact artifact pair, not both")
        return None
    ci_source(ci_ref, run)
    if exact:
        return source_sha, image_run_id
    shared = desktop_pin(desktop, None)
    source = shared["shared_source_sha"]
    if shared["shared_source_kind"] == "generated_client":
        metadata = json.loads(run([
            "gh", "api", f"repos/AppFlowy-IO/AppFlowy-Client/git/commits/{source}",
        ]))
        hint = re.fullmatch(r"sync: update client-api from ([a-f0-9]{7,40})\s*", metadata["message"])
        if hint is None:
            raise ValueError("Generated Client commit has no canonical Server source hint")
        source = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/commits/{hint[1]}"]))["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", source) or not source.startswith(hint[1]):
            raise ValueError("Server source hint did not resolve to its immutable commit")
    # The hint only selects a candidate. Request verification still regenerates
    # and compares Client bytes before any image may be loaded.
    workflow = "build_ci_images_self_hosted.yml"
    listing = json.loads(run([
        "gh", "api", f"repos/{REPOSITORY}/actions/workflows/{workflow}/runs"
        "?event=workflow_dispatch&status=success&per_page=100",
    ]))
    for candidate in listing["workflow_runs"]:
        if f"({source})" not in candidate.get("display_title", ""):
            continue
        identifier = str(candidate["id"])
        build = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{identifier}"]))
        response = json.loads(run([
            "gh", "api", f"repos/{REPOSITORY}/actions/runs/{identifier}/artifacts?per_page=100",
        ]))
        try:
            verify_build(build, source)
            verified_artifacts(response, profile)
        except ValueError:
            continue
        return source, identifier
    raise ValueError(f"No compatible unexpired Cloud image set for {source} among the latest 100 builds")


def prepare_request(source_sha, image_run_id, desktop, cloud, desktop_ref, manifest, run=command, *,
                    client=None, ci_ref="", profile="desktop"):
    if not validate_pair(source_sha, image_run_id):
        return False
    if not re.fullmatch(r"[a-f0-9]{40}", desktop_ref or ""):
        raise ValueError("Exact Cloud mode requires an immutable Desktop pr_ref")
    desktop_sha = run(["git", "rev-parse", "HEAD"], cwd=desktop)
    cloud_sha = run(["git", "rev-parse", "HEAD"], cwd=cloud)
    if desktop_sha != desktop_ref or cloud_sha != source_sha:
        raise ValueError("Checked-out Desktop or Cloud revision differs from the request")
    ci_sha = ci_source(ci_ref, run)
    shared = desktop_pin(desktop, source_sha)
    if shared["shared_source_kind"] == "generated_client":
        shared.update(generated_client_source(cloud, source_sha, client, shared["shared_source_sha"], run))
    build = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{image_run_id}"]))
    verify_build(build, source_sha)
    artifacts = json.loads(run([
        "gh", "api", f"repos/{REPOSITORY}/actions/runs/{image_run_id}/artifacts?per_page=100",
    ]))
    image_artifacts = verified_artifacts(artifacts, profile)
    write_manifest(manifest, {
        "desktop_source_sha": desktop_sha,
        "ci_source_sha": ci_sha,
        "caller_source_sha": os.environ["GITHUB_SHA"],
        "runtime_profile": profile,
        "cloud_source_sha": source_sha,
        "image_run_id": int(image_run_id),
        "image_run_attempt": build["run_attempt"],
        "image_build_url": build["html_url"],
        "image_workflow_sha": build["head_sha"],
        "image_artifacts": {item["name"]: item["id"] for item in image_artifacts},
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
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def verify_image(config, source_sha):
    # Archive configs and docker inspect use different key casing.
    platform = (config.get("os", config.get("Os")), config.get("architecture", config.get("Architecture")))
    labels = config.get("config", config.get("Config", {})).get("Labels") or {}
    if platform != ("linux", "amd64") or labels.get("org.opencontainers.image.revision") != source_sha:
        raise ValueError("Image platform or source revision does not match the request")


def dependency_services(config, profiles=(), protected=None):
    protected = protected if protected is not None else {name: name for name in SERVICES}
    services = config["services"]
    if not set(protected).issubset(services):
        raise ValueError("Compose source does not contain the required Cloud services")
    for service, image in protected.items():
        if services[service].get("image") != image_tag(image):
            raise ValueError(f"Compose image differs from the verified artifact for {service}")
    active = set(profiles)
    return sorted(name for name, value in services.items()
                  if name not in protected and (not value.get("profiles") or "*" in active
                                              or active.intersection(value["profiles"])))


def require_timeline_image(timeline, image):
    if timeline and (not image or image.startswith('-') or any(c.isspace() for c in image)):
        raise ValueError(
            "Timeline requires a prebuilt self-hosted image. "
            "Supply timeline_cloud_image or DESKTOP_TIMELINE_CLOUD_IMAGE; "
            "Desktop CI never builds the server from source."
        )


def gateway_environment(cloud, timeline):
    env = dict(os.environ)
    bypass = cloud / "nginx/api-limits-self-host-ci.conf"
    if timeline and bypass.is_file():
        # The timeline flavor uses self-hosted Cloud, whose HTTP policy must bypass Gateway.
        env["APPFLOWY_CI_API_LIMITS_NGINX_CONFIG"] = str(bypass)
    return env


def start_registry_runtime(cloud, timeline=False, timeline_image="", run=command):
    require_timeline_image(timeline, timeline_image)
    env = gateway_environment(cloud, timeline)
    compose = ["docker", "compose", "-f", str(cloud / "docker-compose-ci.yml")]
    if timeline:
        override = cloud / "desktop-timeline-image.compose.json"
        write_manifest(override, {"services": {"appflowy_cloud": {"image": timeline_image}}})
        compose += ["-f", str(override)]
    # Compose files contain build sections. A missing registry image must fail,
    # never turn a Desktop test into an implicit server compilation.
    run(compose + ["pull", "--quiet"], cwd=cloud, env=env, capture=False)
    run(compose + ["up", "-d", "--no-build", "--pull", "never"], cwd=cloud, env=env, capture=False)


def start_runtime(cloud, archives, manifest, timeline=False, run=command, *, timeline_image="",
                  compose_override=None):
    require_timeline_image(timeline, timeline_image)
    state = json.loads(manifest.read_text())
    source_sha = state["cloud_source_sha"]
    if run(["git", "rev-parse", "HEAD"], cwd=cloud) != source_sha:
        raise ValueError("Cloud checkout changed after request verification")
    # Validate the complete set before the first Docker mutation.
    artifact_names = state.get("image_artifacts", {
        f"docker-image-{name.removeprefix('appflowy_')}": None for name in SERVICES
    })
    known_roles = {f"docker-image-{name.removeprefix('appflowy_')}": name for name in KNOWN_IMAGES}
    if not set(artifact_names).issubset(known_roles):
        raise ValueError("Unknown exact Cloud image role")
    profile = state.get("runtime_profile", "desktop")
    protected_images = runtime_services(profile, (known_roles[name] for name in artifact_names))
    if not set(protected_images.values()).issubset({known_roles[name] for name in artifact_names}):
        raise ValueError("Missing exact Cloud image role")
    if profile == "web" and (compose_override is None or not compose_override.is_file()):
        raise ValueError("Web runtime requires its checked-out Compose override")
    images = {role: verify_archive(archives / f"{role}.tar.gz", role, source_sha)
              for role in (known_roles[name] for name in artifact_names)}
    env = gateway_environment(cloud, timeline)
    for service in images:
        env[f"{service.upper()}_VERSION"] = "latest-amd64"
    compose_name = "docker-compose-web-ci.yml" if profile == "web" else "docker-compose-ci.yml"
    compose = ["docker", "compose", "-f", str(cloud / compose_name)]
    if compose_override is not None:
        compose += ["-f", str(compose_override.resolve())]
    config = json.loads(run(compose + ["config", "--format", "json"], cwd=cloud, env=env))
    dependencies = dependency_services(config, env.get("COMPOSE_PROFILES", "").split(","), protected_images)
    for service, expected_id in images.items():
        run(["docker", "load", "--input", str(archives / f"{service}.tar.gz")], cwd=cloud, capture=False)
        loaded = json.loads(run(["docker", "image", "inspect", image_tag(service)]))[0]
        verify_image(loaded, source_sha)
        if loaded["Id"] != expected_id:
            raise ValueError(f"Loaded image differs from verified archive for {service}")
    if dependencies:
        run(compose + ["pull", "--quiet", *dependencies], cwd=cloud, env=env, capture=False)
    override = manifest.with_suffix(".compose.json")
    protected = {service: {"pull_policy": "never", "image": images[role]}
                 for service, role in protected_images.items()}
    if timeline:
        # The dedicated flavor must already exist and match the exact source.
        # Pin its local ID after pulling so a tag move cannot change the runtime.
        run(["docker", "pull", timeline_image], capture=False)
        pulled = json.loads(run(["docker", "image", "inspect", timeline_image]))[0]
        verify_image(pulled, source_sha)
        images["appflowy_cloud"] = pulled["Id"]
        protected["appflowy_cloud"]["image"] = pulled["Id"]
    write_manifest(override, {"services": protected})
    compose += ["-f", str(override)]
    state.update(stage="images_verified", images=images, timeline_image=timeline_image if timeline else None)
    write_manifest(manifest, state)
    run(compose + ["up", "-d", "--no-build", "--pull", "never"], cwd=cloud, env=env, capture=False)
    # Verify the images actually attached to the containers, not only local tags.
    runtime_images = {service: images[role] for service, role in protected_images.items()}
    for service, expected_id in runtime_images.items():
        container = run(compose + ["ps", "--all", "--quiet", service], cwd=cloud, env=env)
        if not container or "\n" in container:
            raise ValueError(f"Expected exactly one runtime container for {service}")
        actual = json.loads(run(["docker", "inspect", container]))[0]
        if actual["Image"] != expected_id:
            raise ValueError(f"Runtime container uses a different image for {service}")
    state["stage"] = "runtime_verified"
    state["runtime_images"] = runtime_images
    write_manifest(manifest, state)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"Desktop `{state['desktop_source_sha']}` tested against Cloud `{source_sha}` "
                         f"from [image build]({state['image_build_url']}) "
                         f"attempt {state['image_run_attempt']}. Timeline image: {state['timeline_image']}.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    select = sub.add_parser("select")
    select.add_argument("--desktop", type=Path, required=True)
    select.add_argument("--cloud-source-sha", default="")
    select.add_argument("--image-run-id", default="")
    select.add_argument("--cloud-version", default="")
    select.add_argument("--ci-ref", default="")
    select.add_argument("--profile", choices=("desktop", "web"), default="web")
    pins = sub.add_parser("pins")
    pins.add_argument("--cloud-source-sha", default="")
    pins.add_argument("--image-run-id", default="")
    pins.add_argument("--desktop-ref", default="")
    pins.add_argument("--desktop", type=Path, required=True)
    request = sub.add_parser("request")
    request.add_argument("--cloud-source-sha", default="")
    request.add_argument("--image-run-id", default="")
    request.add_argument("--desktop-ref", default="")
    request.add_argument("--desktop", type=Path, required=True)
    request.add_argument("--cloud", type=Path, required=True)
    request.add_argument("--client", type=Path)
    request.add_argument("--ci-ref", default="")
    request.add_argument("--profile", choices=("desktop", "web"), default="desktop")
    request.add_argument("--manifest", type=Path, required=True)
    start = sub.add_parser("start")
    start.add_argument("--cloud", type=Path, required=True)
    start.add_argument("--archives", type=Path, required=True)
    start.add_argument("--manifest", type=Path, required=True)
    start.add_argument("--timeline", action="store_true")
    start.add_argument("--timeline-image", default="")
    start.add_argument("--compose-override", type=Path)
    registry = sub.add_parser("registry")
    registry.add_argument("--cloud", type=Path, required=True)
    registry.add_argument("--timeline", action="store_true")
    registry.add_argument("--timeline-image", default="")
    args = parser.parse_args()
    if args.operation == "select":
        selected = select_prebuilt(args.desktop, args.cloud_source_sha, args.image_run_id,
                                  args.cloud_version, ci_ref=args.ci_ref, profile=args.profile)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"enabled={str(selected is not None).lower()}", file=output)
            if selected:
                print(f"cloud-source-sha={selected[0]}\nimage-run-id={selected[1]}", file=output)
        if selected:
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as environment:
                print(f"CLOUD_REVISION={selected[0]}", file=environment)
    elif args.operation == "pins":
        revision = ""
        if validate_pair(args.cloud_source_sha, args.image_run_id):
            if (not re.fullmatch(r"[a-f0-9]{40}", args.desktop_ref)
                    or command(["git", "rev-parse", "HEAD"], cwd=args.desktop) != args.desktop_ref):
                raise ValueError("Exact Cloud mode requires the checked-out immutable Desktop revision")
            shared = desktop_pin(args.desktop, args.cloud_source_sha)
            if shared["shared_source_kind"] == "generated_client":
                revision = shared["shared_source_sha"]
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"client-sha={revision}", file=output)
    elif args.operation == "request":
        enabled = prepare_request(args.cloud_source_sha, args.image_run_id, args.desktop,
                                  args.cloud, args.desktop_ref, args.manifest, client=args.client,
                                  ci_ref=args.ci_ref, profile=args.profile)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"enabled={str(enabled).lower()}", file=output)
            if enabled:
                state = json.loads(args.manifest.read_text())
                ids = ",".join(str(value) for value in state["image_artifacts"].values())
                print(f"artifact-ids={ids}", file=output)
    elif args.operation == "registry":
        start_registry_runtime(args.cloud.resolve(), args.timeline, args.timeline_image)
    else:
        start_runtime(args.cloud.resolve(), args.archives, args.manifest, args.timeline,
                      timeline_image=args.timeline_image, compose_override=args.compose_override)


if __name__ == "__main__":
    main()
