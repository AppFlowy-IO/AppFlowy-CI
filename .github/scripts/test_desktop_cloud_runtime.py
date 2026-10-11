"""Exact-image orchestration checks without Docker, network or credentials."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

import desktop_cloud_runtime as runtime


SOURCE = "a" * 40
DESKTOP = "b" * 40
CI = "c" * 40
ROOT = Path(__file__).resolve().parents[2]


def build_metadata():
    return {
        "repository": {"full_name": runtime.REPOSITORY},
        "path": runtime.WORKFLOW,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "display_title": f"CI images 900-1 ({SOURCE})",
        "run_attempt": 2,
        "html_url": f"https://github.com/{runtime.REPOSITORY}/actions/runs/123",
        "head_sha": "d" * 40,
    }


def artifacts():
    return {"artifacts": [
        {"name": f"docker-image-{name.removeprefix('appflowy_')}", "id": index, "expired": False}
        for index, name in enumerate(runtime.SERVICES, 1)
    ]}


def write_pins(root, source=SOURCE, extra=""):
    file = root / "frontend/rust-lib/Cargo.toml"
    file.parent.mkdir(parents=True, exist_ok=True)
    pin = f'{{ git = "https://github.com/AppFlowy-IO/AppFlowy-Cloud-Premium.git", rev = "{source}"{extra} }}'
    file.write_text("[workspace.dependencies]\n" + "\n".join(
        f"{name} = {pin}" for name in ("collab", "appflowy-entity", "appflowy-proto")
    ) + f"\n[patch.crates-io]\nclient-api = {pin}\n")


def archive_image(root, service, *, source=SOURCE, architecture="amd64", tag=None, duplicate=False):
    encoded = json.dumps({
        "os": "linux", "architecture": architecture,
        "config": {"Labels": {"org.opencontainers.image.revision": source}},
    }).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    entries = {
        "manifest.json": json.dumps([{
            "Config": f"{digest}.json", "RepoTags": [tag or runtime.image_tag(service)], "Layers": [],
        }]).encode(),
        f"{digest}.json": encoded,
    }
    path = root / f"{service}.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, contents in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
        if duplicate:
            archive.addfile(member, io.BytesIO(contents))
    return "sha256:" + digest


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.desktop = self.root / "desktop"
        self.cloud = self.root / "cloud"
        self.manifest = self.root / "manifest.json"
        self.metadata = build_metadata()
        self.artifacts = artifacts()
        self.calls = []
        write_pins(self.desktop)
        self.env = patch.dict(os.environ, {"GITHUB_SHA": CI})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, args, cwd=None, **_):
        self.calls.append(args)
        if args[0] == "git":
            return DESKTOP if cwd == self.desktop else SOURCE if cwd == self.cloud else CI
        return json.dumps(self.artifacts if "artifacts?" in args[-1] else self.metadata)

    def prepare(self, source=SOURCE, image="123", desktop_ref=DESKTOP):
        return runtime.prepare_request(source, image, self.desktop, self.cloud, desktop_ref,
                                       self.manifest, self.run_command)

    def test_absent_pair_is_legacy_without_any_side_effect(self):
        self.assertFalse(self.prepare("", "", "main"))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.manifest.exists())

    def test_partial_or_mutable_inputs_refuse_before_source_or_network_reads(self):
        for source, image in [(SOURCE, ""), ("", "123"), ("main", "123"), (SOURCE, "0"), (SOURCE, "1\n2")]:
            with self.subTest(source=source, image=image), self.assertRaises(ValueError):
                self.prepare(source, image)
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(ValueError, "immutable Desktop"):
            self.prepare(desktop_ref="main")

    def test_records_all_three_sources_build_attempt_and_exact_artifact_ids(self):
        self.assertTrue(self.prepare())
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest["desktop_source_sha"], DESKTOP)
        self.assertEqual(manifest["cloud_source_sha"], SOURCE)
        self.assertEqual(manifest["ci_source_sha"], CI)
        self.assertEqual(manifest["image_run_attempt"], 2)
        self.assertEqual(set(manifest["image_artifacts"].values()), {1, 2, 3, 4})
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)

    def test_wrong_checkout_or_dependency_pin_refuses_before_github(self):
        with self.assertRaisesRegex(ValueError, "Checked-out"):
            self.prepare(desktop_ref="e" * 40)
        write_pins(self.desktop, "f" * 40)
        with self.assertRaisesRegex(ValueError, "immutable Cloud revision"):
            self.prepare()
        write_pins(self.desktop, extra=', path = "/tmp/mirror"')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertTrue(all(call[0] == "git" for call in self.calls))

    def test_ci_tools_must_match_the_workflow_commit(self):
        with patch.dict(os.environ, {"GITHUB_SHA": "f" * 40}), self.assertRaisesRegex(ValueError, "CI runtime"):
            self.prepare()

    def test_wrong_private_repository_workflow_source_or_attempt_state_refuses(self):
        mutations = [
            ("repository", {"full_name": "someone/other"}), ("path", ".github/workflows/release.yml"),
            ("head_branch", "pr-branch"), ("event", "pull_request"), ("status", "in_progress"),
            ("conclusion", "failure"), ("display_title", f"CI images 900-1 ({'f' * 40})"),
        ]
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.metadata = {**build_metadata(), key: value}
                self.prepare()
        self.assertFalse(self.manifest.exists())

    def test_missing_expired_extra_or_duplicate_image_artifacts_refuse(self):
        bad = []
        missing = artifacts()
        missing["artifacts"].pop()
        bad.append(missing)
        expired = artifacts()
        expired["artifacts"][0]["expired"] = True
        bad.append(expired)
        extra = artifacts()
        extra["artifacts"].append({"name": "docker-image-untrusted", "id": 9, "expired": False})
        bad.append(extra)
        duplicate = artifacts()
        duplicate["artifacts"][0] = duplicate["artifacts"][1]
        bad.append(duplicate)
        for value in bad:
            with self.subTest(artifacts=value), self.assertRaises(ValueError):
                self.artifacts = value
                self.prepare()
        self.assertFalse(self.manifest.exists())

    def test_real_git_source_checkouts_and_manifest_pins(self):
        # Exercise actual Git/TOML reads; only authenticated GitHub metadata is supplied.
        for path in (self.desktop, self.cloud):
            path.mkdir(exist_ok=True)
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "--allow-empty", "-qm", "fixture"], check=True)
        cloud_sha = runtime.command(["git", "rev-parse", "HEAD"], cwd=self.cloud)
        desktop_sha = runtime.command(["git", "rev-parse", "HEAD"], cwd=self.desktop)
        ci_sha = runtime.command(["git", "rev-parse", "HEAD"], cwd=ROOT)
        write_pins(self.desktop, cloud_sha)
        self.metadata["display_title"] = f"CI images 900-1 ({cloud_sha})"

        def run(args, **kwargs):
            return self.run_command(args, **kwargs) if args[0] == "gh" else runtime.command(args, **kwargs)

        with patch.dict(os.environ, {"GITHUB_SHA": ci_sha}):
            self.assertTrue(runtime.prepare_request(cloud_sha, "123", self.desktop, self.cloud,
                                                    desktop_sha, self.manifest, run))


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "manifest.json"
        self.calls = []
        self.images = {name: archive_image(self.root, name) for name in runtime.SERVICES}
        self.config = {"services": {
            **{name: {"image": runtime.image_tag(name)} for name in runtime.SERVICES},
            "postgres": {"image": "postgres:16"}, "nginx": {"image": "nginx"},
            "inactive": {"image": "example", "profiles": ["opt-in"]},
        }}
        self.wrong_container = False
        self.wrong_loaded_image = False
        self.fail_load = False
        self.timeline_id = "sha256:commercial-source-build"
        self.built = False
        runtime.write_manifest(self.manifest, {
            "cloud_source_sha": SOURCE, "desktop_source_sha": DESKTOP,
            "image_build_url": "https://example.invalid/build/123", "image_run_attempt": 1,
        })
        self.env = patch.dict(os.environ, {"COMPOSE_PROFILES": "", "GITHUB_STEP_SUMMARY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "git":
            return SOURCE
        if "config" in args:
            return json.dumps(self.config)
        if args[:2] == ["docker", "load"]:
            if self.fail_load:
                raise RuntimeError("load failed")
            return "loaded"
        if args[:3] == ["docker", "image", "inspect"]:
            service = next(name for name in runtime.SERVICES if args[-1] == runtime.image_tag(name))
            image_id = self.timeline_id if service == "appflowy_cloud" and self.built else self.images[service]
            if self.wrong_loaded_image:
                image_id = "sha256:unexpected-loaded-image"
            return json.dumps([{"Id": image_id, "Os": "linux", "Architecture": "amd64",
                                "Config": {"Labels": {"org.opencontainers.image.revision": SOURCE}}}])
        if args[-2:] == ["build", "appflowy_cloud"]:
            self.assertEqual(kwargs["env"]["APPFLOWY_CLOUD_BUILD_FEATURES"], "self-host-af,commercial-ci,ci-test")
            self.built = True
        if "ps" in args:
            return args[-1]
        if args[:2] == ["docker", "inspect"]:
            image_id = self.timeline_id if args[-1] == "appflowy_cloud" and self.built else self.images[args[-1]]
            return json.dumps([{"Image": "wrong" if self.wrong_container else image_id}])
        return ""

    def start(self, timeline=False):
        runtime.start_runtime(self.root, self.root, self.manifest, timeline, self.run_command)

    def test_real_archives_dependency_only_pull_and_actual_container_ids(self):
        self.start()
        pulls = [args for args, _ in self.calls if "pull" in args]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0][pulls[0].index("pull") + 1:], ["nginx", "postgres"])
        up = next(args for args, _ in self.calls if "up" in args)
        self.assertEqual(up[-5:], ["up", "-d", "--no-build", "--pull", "never"])
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["stage"], "runtime_verified")
        self.assertEqual(state["images"], self.images)

    def test_archive_revision_architecture_tag_and_duplicate_paths_fail_before_docker_mutation(self):
        for changes in ({"source": "e" * 40}, {"architecture": "arm64"}, {"tag": "wrong:latest"}, {"duplicate": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                archive_image(self.root, runtime.SERVICES[-1], **changes)
                self.start()
        self.assertTrue(all(args[0] == "git" for args, _ in self.calls))

    def test_unexpected_compose_image_refuses_before_load_or_pull(self):
        self.config["services"]["appflowy_cloud"]["image"] = "registry/unexpected:latest"
        with self.assertRaisesRegex(ValueError, "Compose image"):
            self.start()
        self.assertFalse(any(args[:2] == ["docker", "load"] for args, _ in self.calls))

    def test_load_failure_never_falls_back_to_registry_or_starts(self):
        self.fail_load = True
        with self.assertRaisesRegex(RuntimeError, "load failed"):
            self.start()
        self.assertFalse(any("pull" in args or "up" in args for args, _ in self.calls))

    def test_wrong_attached_container_image_fails_manifest_confirmation(self):
        self.wrong_container = True
        with self.assertRaisesRegex(ValueError, "Runtime container"):
            self.start()
        self.assertEqual(json.loads(self.manifest.read_text())["stage"], "images_verified")

    def test_wrong_loaded_image_id_refuses_before_dependency_pull_or_up(self):
        self.wrong_loaded_image = True
        with self.assertRaisesRegex(ValueError, "Loaded image differs"):
            self.start()
        self.assertFalse(any("pull" in args or "up" in args for args, _ in self.calls))

    def test_timeline_keeps_same_source_commercial_build_with_revision_label(self):
        self.start(timeline=True)
        override = json.loads(self.manifest.with_suffix(".compose.json").read_text())
        self.assertEqual(override["services"]["appflowy_cloud"]["build"]["labels"],
                         {"org.opencontainers.image.revision": SOURCE})
        self.assertTrue(all(value["pull_policy"] == "never" for value in override["services"].values()))
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["images"]["appflowy_cloud"], self.timeline_id)
        self.assertTrue(state["timeline_source_build"])

    def test_profile_dependencies_are_selected_without_protected_images(self):
        self.assertEqual(runtime.dependency_services(self.config, ["opt-in"]), ["inactive", "nginx", "postgres"])
        self.assertEqual(runtime.dependency_services(self.config, ["*"]), ["inactive", "nginx", "postgres"])


class WorkflowWiringTest(unittest.TestCase):
    def workflow(self, name):
        return yaml.safe_load((ROOT / f".github/workflows/{name}.yaml").read_text())

    def test_receiver_requests_one_image_set_and_forwards_only_to_desktop_runtime_lanes(self):
        job = self.workflow("webhook_receiver")["jobs"]["dispatch-workflows"]
        steps = job["steps"]
        requests = [step for step in steps if "request_image_build.py" in step.get("run", "")]
        self.assertEqual(len(requests), 1)
        self.assertNotIn("CI_TOOLS_SHA", requests[0]["env"])
        self.assertNotIn("PR_NUMBER", requests[0]["env"])
        for name in ("Trigger Flutter CI", "Trigger Rust CI"):
            step = next(step for step in steps if step.get("name") == name)
            self.assertLess(steps.index(requests[0]), steps.index(step))
            self.assertIn('"cloud_source_sha"', step["with"]["client-payload"])
            self.assertIn('"cloud_image_run_id"', step["with"]["client-payload"])
        ios = next(step for step in steps if step.get("name") == "Trigger iOS CI")
        self.assertNotIn("cloud_image_run_id", ios["with"]["client-payload"])

    def test_downstream_source_checkout_precedes_action_and_legacy_cannot_hide_failure(self):
        for workflow, job_name in (("rust_ci", "test"), ("flutter_ci", "cloud_integration_test")):
            value = self.workflow(workflow)
            # PyYAML follows YAML 1.1 and parses the Actions `on` key as True.
            inputs = value[True]["workflow_dispatch"]["inputs"]
            self.assertIn("cloud_source_sha", inputs)
            self.assertIn("cloud_image_run_id", inputs)
            job = value["jobs"][job_name]
            steps = job["steps"]
            exact = next(step for step in steps if step.get("id") == "exact-cloud")
            for name in ("Checkout private repository", "Check out the exact CI runtime tools", "Checkout AppFlowy-Cloud-Premium code"):
                self.assertLess(steps.index(next(step for step in steps if step.get("name") == name)), steps.index(exact))
            legacy = next(step for step in steps if "legacy registry" in step.get("name", ""))
            self.assertEqual(legacy["if"], "steps.exact-cloud.outputs.enabled == 'false'")
            self.assertNotIn("continue-on-error", exact)
            manifest = next(step for step in steps if step.get("name") == "Upload exact Cloud runtime manifest")
            self.assertEqual(manifest["if"], "always()")

    def test_download_uses_verified_artifact_ids_and_exact_private_repository(self):
        action = yaml.safe_load((ROOT / ".github/actions/desktop-cloud-runtime/action.yml").read_text())
        download = next(step for step in action["runs"]["steps"] if step.get("uses", "").startswith("actions/download-artifact"))
        self.assertEqual(download["with"]["artifact-ids"], "${{ steps.request.outputs.artifact-ids }}")
        self.assertEqual(download["with"]["repository"], runtime.REPOSITORY)
        self.assertNotIn("pattern", download["with"])


if __name__ == "__main__":
    unittest.main()
