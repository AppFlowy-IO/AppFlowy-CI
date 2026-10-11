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
import tomllib


REPOSITORY = "AppFlowy-IO/AppFlowy-Cloud-Premium"
WORKFLOW = ".github/workflows/build_ci_images_self_hosted.yml"
SERVICES = ("appflowy_cloud", "appflowy_worker", "appflowy_search", "appflowy_mcp")
SERVER_URL = "https://github.com/appflowy-io/appflowy-cloud-premium"


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


def verify_desktop_pin(desktop, source_sha):
    with (desktop / "frontend/rust-lib/Cargo.toml").open("rb") as handle:
        manifest = tomllib.load(handle)
    dependencies = manifest["workspace"]["dependencies"]
    pins = {name: dependencies[name] for name in ("collab", "appflowy-entity", "appflowy-proto")}
    pins["client-api"] = manifest["patch"]["crates-io"]["client-api"]
    for name, pin in pins.items():
        if (not isinstance(pin, dict) or "path" in pin
                or pin.get("git", "").removesuffix(".git").lower() != SERVER_URL
                or pin.get("rev") != source_sha):
            raise ValueError(f"Desktop {name} must use the requested immutable Cloud revision")


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


def prepare_request(source_sha, image_run_id, desktop, cloud, desktop_ref, manifest, run=command):
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
    verify_desktop_pin(desktop, source_sha)
    build = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{image_run_id}"]))
    verify_build(build, source_sha)
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
        "stage": "request_verified",
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


def dependency_services(config, profiles=()):
    services = config["services"]
    if not set(SERVICES).issubset(services):
        raise ValueError("Compose source does not contain the required Cloud services")
    for service in SERVICES:
        if services[service].get("image") != image_tag(service):
            raise ValueError(f"Compose image differs from the verified artifact for {service}")
    active = set(profiles)
    return sorted(name for name, value in services.items()
                  if name not in SERVICES and (not value.get("profiles") or "*" in active
                                              or active.intersection(value["profiles"])))


def start_runtime(cloud, archives, manifest, timeline=False, run=command):
    state = json.loads(manifest.read_text())
    source_sha = state["cloud_source_sha"]
    if run(["git", "rev-parse", "HEAD"], cwd=cloud) != source_sha:
        raise ValueError("Cloud checkout changed after request verification")
    # Validate the complete set before the first Docker mutation.
    images = {service: verify_archive(archives / f"{service}.tar.gz", service, source_sha)
              for service in SERVICES}
    env = dict(os.environ)
    for service in SERVICES:
        env[f"{service.upper()}_VERSION"] = "latest-amd64"
    if timeline:
        env["APPFLOWY_CLOUD_BUILD_FEATURES"] = "self-host-af,commercial-ci,ci-test"
    compose = ["docker", "compose", "-f", str(cloud / "docker-compose-ci.yml")]
    config = json.loads(run(compose + ["config", "--format", "json"], cwd=cloud, env=env))
    dependencies = dependency_services(config, env.get("COMPOSE_PROFILES", "").split(","))
    for service, expected_id in images.items():
        run(["docker", "load", "--input", str(archives / f"{service}.tar.gz")], cwd=cloud, capture=False)
        loaded = json.loads(run(["docker", "image", "inspect", image_tag(service)]))[0]
        verify_image(loaded, source_sha)
        if loaded["Id"] != expected_id:
            raise ValueError(f"Loaded image differs from verified archive for {service}")
    if dependencies:
        run(compose + ["pull", *dependencies], cwd=cloud, env=env, capture=False)
    override = manifest.with_suffix(".compose.json")
    protected = {service: {"pull_policy": "never"} for service in SERVICES}
    if timeline:
        # Preserve the Timeline lane's commercial self-hosted contract while
        # building it from exactly the same source as the other three images.
        protected["appflowy_cloud"]["build"] = {
            "labels": {"org.opencontainers.image.revision": source_sha},
        }
    write_manifest(override, {"services": protected})
    compose += ["-f", str(override)]
    if timeline:
        run(compose + ["build", "appflowy_cloud"], cwd=cloud, env=env, capture=False)
        built = json.loads(run(["docker", "image", "inspect", image_tag("appflowy_cloud")]))[0]
        verify_image(built, source_sha)
        images["appflowy_cloud"] = built["Id"]
    state.update(stage="images_verified", images=images, timeline_source_build=timeline)
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
    request = sub.add_parser("request")
    request.add_argument("--cloud-source-sha", default="")
    request.add_argument("--image-run-id", default="")
    request.add_argument("--desktop-ref", default="")
    request.add_argument("--desktop", type=Path, required=True)
    request.add_argument("--cloud", type=Path, required=True)
    request.add_argument("--manifest", type=Path, required=True)
    start = sub.add_parser("start")
    start.add_argument("--cloud", type=Path, required=True)
    start.add_argument("--archives", type=Path, required=True)
    start.add_argument("--manifest", type=Path, required=True)
    start.add_argument("--timeline", action="store_true")
    args = parser.parse_args()
    if args.operation == "request":
        enabled = prepare_request(args.cloud_source_sha, args.image_run_id, args.desktop,
                                  args.cloud, args.desktop_ref, args.manifest)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"enabled={str(enabled).lower()}", file=output)
            if enabled:
                state = json.loads(args.manifest.read_text())
                ids = ",".join(str(value) for value in state["image_artifacts"].values())
                print(f"artifact-ids={ids}", file=output)
    else:
        start_runtime(args.cloud.resolve(), args.archives, args.manifest, args.timeline)


if __name__ == "__main__":
    main()
