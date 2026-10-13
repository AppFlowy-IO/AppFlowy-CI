"""Exact-image orchestration checks without Docker, network or credentials."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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


def release_log(service, source=SOURCE, **changes):
    # Same final step group shape as the published workflow's timestamped logs.
    values = {"SOURCE_SHA": source, "IMAGE": f"***/{service}_premium:0.19.29",
              "IMAGE_NAME": f"***/{service}_premium", "PLATFORMS": "linux/arm64,linux/amd64",
              "PUBLISHED": "true", "DIGEST": "sha256:" + "e" * 64, **changes}
    return "\n".join("2026-10-13T03:03:12.0947925Z " + line for line in [
        "##[group]Run {", "printf 'Published digest'", "env:",
        *[f"  {key}: {value}" for key, value in values.items()], "##[endgroup]",
    ])


def release_build():
    return {**build_metadata(), "path": runtime.RELEASE_WORKFLOW,
            "display_title": "Self-hosted Docker publish 0.19.29 from main (linux/arm64,linux/amd64)"}


def write_pins(root, source=SOURCE, extra="", repository=runtime.SERVER_URL):
    file = root / "frontend/rust-lib/Cargo.toml"
    file.parent.mkdir(parents=True, exist_ok=True)
    pin = f'{{ git = "{repository}.git", rev = "{source}"{extra} }}'
    file.write_text("[workspace.dependencies]\n" + "\n".join(
        f"{name} = {pin}" for name in ("collab", "appflowy-entity", "appflowy-proto")
    ) + f"\n[patch.crates-io]\nclient-api = {pin}\n")
    file.with_name("Cargo.lock").write_text("version = 4\n" + "".join(
        f'\n[[package]]\nname = "{name}"\nversion = "0.1.0"\n'
        f'source = "git+{repository}.git?rev={source}#{source}"\n'
        for name in ("collab", "appflowy-entity", "appflowy-proto", "client-api")
    ))


def archive_image(root, service, *, source=SOURCE, architecture="amd64", tag=None, duplicate=False, oci=None):
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
    if oci is not None:
        config_path = "blobs/sha256/" + digest
        entries[config_path] = entries.pop(f"{digest}.json")
        docker_manifest = json.loads(entries["manifest.json"])
        docker_manifest[0]["Config"] = config_path
        entries["manifest.json"] = json.dumps(docker_manifest).encode()
        oci_manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                   "digest": "sha256:" + digest, "size": len(encoded)}, "layers": []}
        if oci == "wrong_config":
            oci_manifest["config"]["digest"] = "sha256:" + "f" * 64
        elif oci == "wrong_config_size":
            oci_manifest["config"]["size"] += 1
        manifest_bytes = json.dumps(oci_manifest).encode()
        manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        entries["blobs/" + manifest_digest.replace(":", "/")] = manifest_bytes + (b" " if oci == "tampered_digest" else b"")
        descriptor = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": manifest_digest,
                      "size": len(manifest_bytes), "platform": {"os": "linux", "architecture": "amd64"},
                      "annotations": {"io.containerd.image.name": "docker.io/" + runtime.image_tag(service),
                                      "org.opencontainers.image.ref.name": "latest-amd64"}}
        if oci == "tampered_digest":
            descriptor["size"] += 1
        elif oci == "wrong_platform":
            descriptor["platform"]["architecture"] = "arm64"
        elif oci == "wrong_tag":
            descriptor["annotations"]["io.containerd.image.name"] = "docker.io/other/image:latest-amd64"
        elif oci == "different_layers":
            docker_manifest = json.loads(entries["manifest.json"])
            docker_manifest[0]["Layers"] = ["different-layer.tar"]
            entries["manifest.json"] = json.dumps(docker_manifest).encode()
        entries["index.json"] = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                                           "manifests": [descriptor] * (2 if oci == "multiple" else 1)}).encode()
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


class GeneratedClientTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cloud = self.root / "cloud"
        self.client = self.root / "client"
        self.desktop = self.root / "desktop"
        for repository in (self.cloud, self.client, self.desktop):
            runtime.command(["git", "init", "-q", str(repository)])
        files = {
            "Cargo.toml": '[workspace]\nmembers = ["libs/client-api"]\n',
            "libs/client-api/Cargo.toml": '[package]\nname = "client-api"\nversion = "0.1.0"\n',
            "libs/client-api/src/lib.rs": "pub fn current_protocol() {}\n",
            "vendor/parser/src/lib.rs": "pub fn bounded_parser() {}\n",
            "tests/yrs_test/files/corrupted-doc.bin": "retained regression fixture",
        }
        for name, content in files.items():
            path = self.cloud / "export" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        script = self.cloud / "scripts/sync_client_repo.sh"
        script.parent.mkdir()
        script.write_text('#!/usr/bin/env bash\nset -euo pipefail\ncp -R "$1/export/." "$2/"\n')
        self.source = self.commit(self.cloud)
        shutil.copytree(self.cloud / "export", self.client, dirs_exist_ok=True)
        (self.client / "Cargo.lock").write_text("standalone resolution is not the downstream lock\n")
        self.client_sha = self.commit(self.client)
        runtime.command(["git", "remote", "add", "origin", runtime.CLIENT_URL + ".git"], cwd=self.client)
        write_pins(self.desktop, self.client_sha, repository=runtime.CLIENT_URL)
        self.desktop_sha = self.commit(self.desktop)

    def commit(self, repository):
        runtime.command(["git", "add", "-A"], cwd=repository)
        runtime.command(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "--allow-empty", "-qm", "fixture"], cwd=repository)
        return runtime.command(["git", "rev-parse", "HEAD"], cwd=repository)

    def verify(self, client_sha=None):
        return runtime.generated_client_source(self.cloud, self.source, self.client,
                                               client_sha or self.client_sha)

    def test_real_git_generated_source_is_bound_to_request_before_images(self):
        requests = []
        metadata = {**build_metadata(), "display_title": f"CI images 900-1 ({self.source})"}

        def run(args, **kwargs):
            if args[0] == "gh":
                requests.append(args)
                return json.dumps(artifacts() if "artifacts?" in args[-1] else metadata)
            return runtime.command(args, **kwargs)

        manifest = self.root / "manifest.json"
        ci_sha = runtime.command(["git", "rev-parse", "HEAD"], cwd=ROOT)
        with patch.dict(os.environ, {"GITHUB_SHA": ci_sha}):
            self.assertTrue(runtime.prepare_request(self.source, "123", self.desktop, self.cloud,
                            self.desktop_sha, manifest, run, client=self.client))
        state = json.loads(manifest.read_text())
        self.assertEqual(state["shared_source_kind"], "generated_client")
        self.assertEqual(state["client_source_sha"], self.client_sha)
        self.assertEqual(state["cloud_source_sha"], self.source)
        self.assertRegex(state["generated_source_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(state["sync_script_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(len(requests), 2)

        (self.client / "libs/client-api/src/lib.rs").write_text("unrelated source\n")
        changed = self.commit(self.client)
        write_pins(self.desktop, changed, repository=runtime.CLIENT_URL)
        desktop_sha = self.commit(self.desktop)
        requests.clear()
        manifest.unlink()
        with patch.dict(os.environ, {"GITHUB_SHA": ci_sha}), self.assertRaisesRegex(ValueError, "source differs"):
            runtime.prepare_request(self.source, "123", self.desktop, self.cloud,
                                    desktop_sha, manifest, run, client=self.client)
        self.assertEqual(requests, [])
        self.assertFalse(manifest.exists())

    def test_mixed_repository_revision_and_unsupported_pins_are_rejected(self):
        manifest = self.desktop / "frontend/rust-lib/Cargo.toml"
        original = manifest.read_text()
        for changed in (original.replace(runtime.CLIENT_URL, runtime.SERVER_URL, 1),
                        original.replace(self.client_sha, "f" * 40, 1),
                        original.replace(runtime.CLIENT_URL, "https://github.com/other/client")):
            with self.subTest(manifest=changed), self.assertRaises(ValueError):
                manifest.write_text(changed)
                runtime.desktop_pin(self.desktop, self.source)

    def test_wrong_or_missing_resolved_lock_source_is_rejected(self):
        lock = self.desktop / "frontend/rust-lib/Cargo.lock"
        original = lock.read_text()
        for changed in (original.replace("#" + self.client_sha, "#" + "f" * 40, 1),
                        original.replace(runtime.CLIENT_URL, runtime.SERVER_URL, 1),
                        original.replace('name = "client-api"', 'name = "other"')):
            with self.subTest(lock=changed), self.assertRaisesRegex(ValueError, "lockfile"):
                lock.write_text(changed)
                runtime.desktop_pin(self.desktop, self.source)

    def test_wrong_checkout_or_origin_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "canonical pinned revision"):
            self.verify("f" * 40)
        runtime.command(["git", "remote", "set-url", "origin", "https://github.com/other/client"], cwd=self.client)
        with self.assertRaisesRegex(ValueError, "canonical pinned revision"):
            self.verify()

    def test_all_generated_files_and_executable_modes_are_compared(self):
        mutations = [
            ("Cargo.toml", "different workspace\n"),
            ("libs/client-api/src/lib.rs", "different protocol\n"),
            ("vendor/parser/src/lib.rs", "different parser\n"),
            ("tests/yrs_test/files/corrupted-doc.bin", "different fixture"),
            ("extra.rs", "unexpected input"),
        ]
        for name, content in mutations:
            with self.subTest(path=name):
                runtime.command(["git", "reset", "--hard", self.client_sha], cwd=self.client)
                (self.client / name).write_text(content)
                with self.assertRaisesRegex(ValueError, "source differs"):
                    self.verify(self.commit(self.client))
        runtime.command(["git", "reset", "--hard", self.client_sha], cwd=self.client)
        (self.client / "vendor/parser/src/lib.rs").unlink()
        with self.assertRaisesRegex(ValueError, "source differs"):
            self.verify(self.commit(self.client))
        runtime.command(["git", "reset", "--hard", self.client_sha], cwd=self.client)
        (self.client / "libs/client-api/src/lib.rs").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "source differs"):
            self.verify(self.commit(self.client))

    def test_source_equivalent_metadata_and_standalone_lock_are_not_authorship_claims(self):
        before = self.verify()
        (self.client / "Cargo.lock").write_text("new standalone-only resolution\n")
        after = self.verify(self.commit(self.client))
        self.assertNotEqual(before["client_source_sha"], after["client_source_sha"])
        self.assertEqual(before["generated_source_sha256"], after["generated_source_sha256"])

    def proto_fixture(self):
        name = "libs/appflowy-proto/src/pb/messages.rs"
        for root in (self.cloud / "export", self.client):
            generated = root / name
            generated.parent.mkdir(parents=True)
            generated.write_text('// This file is @generated by prost-build.\n'
                                 '#[derive(Clone, ::prost::Message)]\npub struct Body {\n'
                                 '  #[prost(string, tag = "1")]\n  pub text: String,\n}\n')
            schema = root / "libs/appflowy-proto/proto/messages.proto"
            schema.parent.mkdir(parents=True)
            schema.write_text('syntax = "proto3";\npackage messages;\nmessage Body { string text = 1; }\n')
        (self.cloud / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.98.0"\n')
        (self.cloud / "rustfmt.toml").write_text('tab_spaces = 2\nedition = "2024"\n')
        self.source = self.commit(self.cloud)
        generated = self.client / name
        generated.write_text(generated.read_text().replace("\n  ", "\n    "))
        self.client_sha = self.commit(self.client)
        return name

    def test_only_schema_backed_prost_formatting_is_canonicalized_with_source_toolchain(self):
        name = self.proto_fixture()
        proof = self.verify()
        self.assertEqual(proof["generated_proto_format_toolchain"], "1.98.0")
        self.assertEqual(proof["generated_proto_format_paths"], [name])
        self.assertRegex(proof["generated_proto_format_config_sha256"], r"^[a-f0-9]{64}$")
        # The check formats only disposable exports, never the user's checkout.
        self.assertIn("\n    pub text", (self.client / name).read_text())

    def test_prost_wire_schema_authored_source_and_modes_stay_strict(self):
        name = self.proto_fixture()
        mutations = [
            (name, lambda text: text.replace('tag = "1"', 'tag = "2"')),
            (name, lambda text: text.replace("String", "u64")),
            (name, lambda text: text.replace("@generated by prost-build", "hand authored")),
            ("libs/appflowy-proto/proto/messages.proto", lambda text: text.replace("text = 1", "text = 2")),
            ("libs/client-api/src/lib.rs", lambda text: text.replace("{}", "{ }")),
        ]
        for relative, change in mutations:
            with self.subTest(relative=relative):
                runtime.command(["git", "reset", "--hard", self.client_sha], cwd=self.client)
                file = self.client / relative
                file.write_text(change(file.read_text()))
                with self.assertRaisesRegex(ValueError, "source differs"):
                    self.verify(self.commit(self.client))
        runtime.command(["git", "reset", "--hard", self.client_sha], cwd=self.client)
        (self.client / name).chmod(0o755)
        with self.assertRaisesRegex(ValueError, "source differs"):
            self.verify(self.commit(self.client))

    def test_unavailable_or_wrong_source_formatter_fails_clearly(self):
        self.proto_fixture()

        def missing(args, **kwargs):
            if args[0] == "rustup":
                raise subprocess.CalledProcessError(1, args)
            return runtime.command(args, **kwargs)

        with self.assertRaisesRegex(ValueError, "Install rustfmt for source-pinned Rust 1.98.0"):
            runtime.generated_client_source(self.cloud, self.source, self.client, self.client_sha, missing)

        def wrong(args, **kwargs):
            return "rustc 1.92.0 (fixture)" if args[0] == "rustup" else runtime.command(args, **kwargs)

        with self.assertRaisesRegex(ValueError, "different Rust toolchain"):
            runtime.generated_client_source(self.cloud, self.source, self.client, self.client_sha, wrong)

    def test_formatter_cannot_normalize_authored_modules_reached_from_generated_file(self):
        name = self.proto_fixture()
        for root in (self.cloud / "export", self.client):
            file = root / name
            file.write_text(file.read_text() + '#[path = "../authored.rs"]\npub mod authored;\n')
            authored = root / "libs/appflowy-proto/src/authored.rs"
            authored.write_text("pub fn authored() {\n    let _ = 1;\n}\n")
        self.source = self.commit(self.cloud)
        self.client_sha = self.commit(self.client)
        with self.assertRaisesRegex(ValueError, "outside the allowed modules"):
            self.verify()

    def test_generation_uses_committed_cloud_bytes_and_cleans_temporary_files_on_failure(self):
        (self.cloud / "export/libs/client-api/src/lib.rs").write_text("uncommitted job edit\n")
        self.verify()
        temporary_directory = tempfile.TemporaryDirectory
        created = []

        def owned_directory(*args, **kwargs):
            directory = temporary_directory(*args, **kwargs)
            created.append(Path(directory.name))
            return directory

        with patch.object(runtime.tempfile, "TemporaryDirectory", side_effect=owned_directory):
            (self.client / "extra.rs").write_text("unexpected input")
            with self.assertRaisesRegex(ValueError, "source differs"):
                self.verify(self.commit(self.client))
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())

    def test_pin_cli_only_requests_client_checkout_for_generated_exact_mode(self):
        output = self.root / "outputs"
        arguments = [sys.executable, "-B", str(ROOT / ".github/scripts/desktop_cloud_runtime.py"),
                     "pins", "--desktop", str(self.desktop), "--desktop-ref", self.desktop_sha]
        environment = {**os.environ, "GITHUB_OUTPUT": str(output)}
        exact = ["--cloud-source-sha", self.source, "--image-run-id", "123"]
        subprocess.run(arguments + exact, env=environment, check=True, capture_output=True)
        self.assertEqual(output.read_text(), f"client-sha={self.client_sha}\nshared-cloud-sha=\n")
        output.unlink()
        write_pins(self.desktop, self.source)
        arguments[-1] = self.commit(self.desktop)
        subprocess.run(arguments + exact, env=environment, check=True, capture_output=True)
        self.assertEqual(output.read_text(), "client-sha=\nshared-cloud-sha=\n")
        output.unlink()
        # Legacy mode does not inspect source pins or require even a checkout.
        shutil.rmtree(self.desktop)
        subprocess.run(arguments, env=environment, check=True, capture_output=True)
        self.assertEqual(output.read_text(), "client-sha=\nshared-cloud-sha=\n")


class ReleaseImageTest(unittest.TestCase):
    def test_only_successful_published_amd64_release_workflow_is_supported(self):
        self.assertEqual(runtime.verify_build(release_build(), SOURCE), "release_registry")
        tag = {**release_build(), "event": "push", "head_branch": "0.19.29", "head_sha": SOURCE,
               "display_title": "Self-hosted Docker publish 0.19.29 from 0.19.29 (linux/arm64,linux/amd64)"}
        self.assertEqual(runtime.verify_build(tag, SOURCE), "release_registry")
        for changes in ({"head_sha": "f" * 40}, {"head_branch": "feature"}, {"event": "pull_request"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runtime.verify_build({**tag, **changes}, SOURCE)
        for key, value in (("display_title", "Self-hosted Docker build only 0.19.29 from main (linux/arm64,linux/amd64)"),
                           ("display_title", "Self-hosted Docker publish 0.19.29 from main (linux/arm64)"),
                           ("conclusion", "failure"), ("status", "in_progress"),
                           ("path", ".github/workflows/untrusted.yml")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                runtime.verify_build({**release_build(), key: value}, SOURCE)

    def test_run_log_proves_source_service_and_digest_despite_masked_namespace(self):
        proof = runtime.release_image_proof(release_log("appflowy_cloud"), "appflowy_cloud", SOURCE)
        self.assertEqual(proof["reference"], "appflowyinc/appflowy_cloud_premium@sha256:" + "e" * 64)
        self.assertEqual(proof["published_tag"], "appflowyinc/appflowy_cloud_premium:0.19.29")
        unmasked = release_log("appflowy_cloud").replace("***/", "appflowyinc/")
        self.assertEqual(runtime.release_image_proof(unmasked, "appflowy_cloud", SOURCE), proof)

    def test_malformed_ambiguous_or_wrong_release_proofs_are_rejected(self):
        bad = [release_log("appflowy_worker"), "", release_log("appflowy_cloud") * 2,
               release_log("appflowy_cloud").replace("  DIGEST:", "  DIFFERENT:")]
        for changes in ({"SOURCE_SHA": "f" * 40}, {"PUBLISHED": "false"}, {"PLATFORMS": "linux/arm64"},
                        {"DIGEST": "latest"}, {"IMAGE_NAME": "other/appflowy_cloud_premium"},
                        {"IMAGE": "***/different:0.19.29"}):
            bad.append(release_log("appflowy_cloud", **changes))
        for log in bad:
            with self.subTest(log=log), self.assertRaises(ValueError):
                runtime.release_image_proof(log, "appflowy_cloud", SOURCE)

    def test_job_log_capture_supports_escape_guard_without_weakening_other_api_reads(self):
        jobs = [{"id": index, "name": f"Build and publish {service}", "status": "completed",
                 "conclusion": "success", "steps": [{"name": "Record the result", "conclusion": "success"}]}
                for index, service in enumerate(runtime.SERVICES)]
        for supports_guard in (True, False):
            calls = []

            def run(args, **_):
                calls.append(args)
                if args == ["gh", "api", "--help"]:
                    return "--allow-escape-sequences" if supports_guard else "Legacy gh api options"
                if args[-1].endswith("/logs"):
                    if supports_guard and "--allow-escape-sequences" not in args:
                        raise subprocess.CalledProcessError(1, args, stderr="the response contains terminal escape sequences; pass --allow-escape-sequences to output it anyway")
                    if not supports_guard and "--allow-escape-sequences" in args:
                        raise subprocess.CalledProcessError(1, args, stderr="unknown flag: --allow-escape-sequences")
                    service = jobs[int(args[-1].split("/")[-2])]["name"].removeprefix("Build and publish ")
                    return release_log(service).replace("Run {", "\x1b[36;1mRun {\x1b[0m")
                self.assertNotIn("--allow-escape-sequences", args)
                return json.dumps({"jobs": jobs})

            with self.subTest(supports_guard=supports_guard):
                result = runtime.release_images("123", release_build(), SOURCE, run)
                self.assertEqual(set(result), set(runtime.SERVICES))
                self.assertEqual(calls.count(["gh", "api", "--help"]), 1)
                self.assertEqual(sum(args[-1].endswith("/logs") for args in calls), len(runtime.SERVICES))

    def test_job_log_download_failure_is_not_retried_without_the_guard(self):
        jobs = [{"id": index, "name": f"Build and publish {service}", "status": "completed",
                 "conclusion": "success", "steps": [{"name": "Record the result", "conclusion": "success"}]}
                for index, service in enumerate(runtime.SERVICES)]
        downloads = []

        def run(args, **_):
            if args == ["gh", "api", "--help"]:
                return "--allow-escape-sequences"
            if args[-1].endswith("/logs"):
                downloads.append(args)
                raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: access denied")
            return json.dumps({"jobs": jobs})

        with self.assertRaises(subprocess.CalledProcessError):
            runtime.release_images("123", release_build(), SOURCE, run)
        self.assertEqual(len(downloads), 1)
        self.assertIn("--allow-escape-sequences", downloads[0])

    def test_complete_request_reads_only_the_successful_attempt_and_records_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop, cloud, manifest = root / "desktop", root / "cloud", root / "manifest"
            write_pins(desktop)
            jobs = [{"id": index, "name": f"Build and publish {service}", "status": "completed",
                     "conclusion": "success", "steps": [{"name": "Record the result", "conclusion": "success"}]}
                    for index, service in enumerate((*runtime.SERVICES, "gotrue"))]
            calls = []

            def run(args, cwd=None, **_):
                calls.append(args)
                if args[0] == "git":
                    return DESKTOP if cwd == desktop else SOURCE if cwd == cloud else CI
                if args[-1].endswith("/logs"):
                    return release_log(jobs[int(args[-1].split("/")[-2])]["name"].removeprefix("Build and publish "))
                return json.dumps({"jobs": jobs} if "/jobs?" in args[-1] else release_build())

            with patch.dict(os.environ, {"GITHUB_SHA": CI}):
                runtime.prepare_request(SOURCE, "123", desktop, cloud, DESKTOP, manifest, run)
                state = json.loads(manifest.read_text())
                self.assertEqual(state["image_kind"], "release_registry")
                self.assertEqual(state["image_artifacts"], {})
                self.assertEqual(set(state["release_images"]), {*runtime.SERVICES, "gotrue"})
                self.assertTrue(any("/attempts/2/jobs?" in call[-1] for call in calls))
                self.assertFalse(any("artifacts?" in call[-1] for call in calls))
                for mutation in (lambda: jobs.pop(0), lambda: jobs.append(jobs[0])):
                    saved = jobs[:]
                    mutation()
                    with self.assertRaises(ValueError):
                        runtime.prepare_request(SOURCE, "123", desktop, cloud, DESKTOP, manifest, run)
                    jobs[:] = saved
                jobs[0]["conclusion"] = "failure"
                with self.assertRaises(ValueError):
                    runtime.prepare_request(SOURCE, "123", desktop, cloud, DESKTOP, manifest, run)


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
        self.wrong_digest = False
        self.wrong_revision = False
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
            service = next(name for name in self.images if args[-1] == runtime.image_tag(name)
                           or f"/{name}_premium@" in args[-1])
            image_id = self.timeline_id if service == "appflowy_cloud" and self.built else self.images[service]
            if self.wrong_loaded_image:
                image_id = "sha256:unexpected-loaded-image"
            return json.dumps([{"Id": image_id, "Os": "linux", "Architecture": "amd64",
                                "RepoDigests": [] if self.wrong_digest else [args[-1]],
                                "Config": {"Labels": {"org.opencontainers.image.revision": "f" * 40 if self.wrong_revision else SOURCE}}}])
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

    def test_classic_and_containerd_ids_are_bound_to_the_same_verified_archive(self):
        for store in ("config_id", "manifest_id"):
            with self.subTest(store=store):
                for service in runtime.SERVICES:
                    archive_image(self.root, service, oci="valid")
                    proof = runtime.verify_archive(self.root / f"{service}.tar.gz", service, SOURCE)
                    self.images[service] = proof[store]
                self.start()
                state = json.loads(self.manifest.read_text())
                self.assertEqual(state["images"], self.images)
                self.assertEqual(state["stage"], "runtime_verified")
                self.assertTrue(all(store in value for value in state["archive_image_proofs"].values()))

    def test_oci_identity_must_prove_one_manifest_same_config_and_layers(self):
        for variant in ("wrong_config", "wrong_config_size", "tampered_digest", "wrong_platform", "wrong_tag", "different_layers", "multiple"):
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                archive_image(self.root, runtime.SERVICES[-1], oci=variant)
                self.start()
        self.assertTrue(all(args[0] == "git" for args, _ in self.calls))

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

    def release_manifest(self):
        state = json.loads(self.manifest.read_text())
        state.update(image_kind="release_registry", release_images={
            service: {"reference": f"appflowyinc/{service}_premium@sha256:" + "e" * 64}
            for service in (*runtime.SERVICES, "gotrue")})
        self.images["gotrue"] = "sha256:" + "f" * 64
        for service, proof in state["release_images"].items():
            self.config["services"][service] = {"image": proof["reference"]}
        runtime.write_manifest(self.manifest, state)
        return state

    def test_release_pulls_only_verified_immutable_digests_and_pins_gotrue_too(self):
        state = self.release_manifest()
        self.start()
        pulls = [args for args, _ in self.calls if args[:2] == ["docker", "pull"]]
        self.assertEqual(pulls, [["docker", "pull", "--platform", "linux/amd64", proof["reference"]]
                                 for proof in state["release_images"].values()])
        self.assertFalse(any(args[:2] == ["docker", "load"] for args, _ in self.calls))
        dependency_pull = next(args for args, _ in self.calls if "compose" in args and "pull" in args)
        self.assertEqual(dependency_pull[-3:], ["pull", "nginx", "postgres"])
        override = json.loads(self.manifest.with_suffix(".compose.json").read_text())
        for service, proof in state["release_images"].items():
            self.assertEqual(override["services"][service], {"image": proof["reference"],
                             "platform": "linux/amd64", "pull_policy": "never"})
        self.assertEqual(json.loads(self.manifest.read_text())["stage"], "runtime_verified")

    def test_incomplete_release_or_mutable_reference_refuses_before_docker(self):
        state = self.release_manifest()
        for change in (lambda: state["release_images"].pop("appflowy_cloud"),
                       lambda: state["release_images"]["appflowy_cloud"].update(reference="appflowyinc/appflowy_cloud_premium:latest")):
            state = self.release_manifest()
            change()
            runtime.write_manifest(self.manifest, state)
            with self.assertRaisesRegex(ValueError, "complete immutable image"):
                self.start()
        self.assertTrue(all(args[0] == "git" for args, _ in self.calls))

    def test_wrong_registry_digest_or_source_fails_without_start_or_fallback(self):
        for flag in ("wrong_digest", "wrong_revision"):
            with self.subTest(flag=flag):
                self.release_manifest()
                self.calls.clear()
                setattr(self, flag, True)
                with self.assertRaises(ValueError):
                    self.start()
                setattr(self, flag, False)
                self.assertFalse(any("up" in args or args[:2] == ["docker", "load"] for args, _ in self.calls))

    def test_release_timeline_remains_explicit_source_build_and_protects_other_digests(self):
        self.release_manifest()
        self.config["services"]["appflowy_cloud"]["image"] = runtime.image_tag("appflowy_cloud")
        self.start(timeline=True)
        state = json.loads(self.manifest.read_text())
        self.assertTrue(state["timeline_source_build"])
        self.assertEqual(state["images"]["appflowy_cloud"], self.timeline_id)


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
        self.assertEqual(download["if"], "steps.request.outputs.enabled == 'true' && steps.request.outputs.image-kind == 'ci_archive'")
        self.assertNotIn("pattern", download["with"])

    def test_generated_client_checkout_is_conditional_canonical_and_precedes_verification(self):
        action = yaml.safe_load((ROOT / ".github/actions/desktop-cloud-runtime/action.yml").read_text())
        steps = action["runs"]["steps"]
        checkout = next(step for step in steps if step.get("with", {}).get("repository") == "AppFlowy-IO/AppFlowy-Client")
        self.assertEqual(checkout["if"], "steps.pins.outputs.client-sha != ''")
        self.assertEqual(checkout["with"]["repository"], "AppFlowy-IO/AppFlowy-Client")
        self.assertEqual(checkout["with"]["ref"], "${{ steps.pins.outputs.client-sha }}")
        self.assertFalse(checkout["with"]["persist-credentials"])
        request = next(step for step in steps if step.get("id") == "request")
        self.assertLess(steps.index(checkout), steps.index(request))
        formatter = next(step for step in steps if step.get("name") == "Install the source-pinned generated protobuf formatter")
        self.assertLess(steps.index(formatter), steps.index(request))
        self.assertEqual(formatter["if"], "steps.pins.outputs.client-sha != ''")
        self.assertIn('rustup toolchain install "$toolchain" --profile minimal --component rustfmt', formatter["run"])
        self.assertNotIn("rustup default", formatter["run"])
        self.assertIn('--client "$GITHUB_WORKSPACE/.ci-generated-client"', request["run"])


class EquivalentServerSourceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cloud = self.root / "runtime"
        self.shared = self.root / "shared"
        runtime.command(["git", "init", "-q", str(self.cloud)])
        members = [f"libs/{name}" for name in ("client-api", "collab", "appflowy-entity", "appflowy-proto")]
        for member in members:
            crate = self.cloud / "export" / member
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
        (self.cloud / "export/Cargo.toml").write_text("[workspace]\nmembers = " + json.dumps(members) + "\n")
        (self.cloud / "export/libs/client-api/lib.rs").write_text("pub fn unchanged() {}\n")
        script = self.cloud / "scripts/sync_client_repo.sh"
        script.parent.mkdir()
        script.write_text('#!/usr/bin/env bash\nset -euo pipefail\ncp -R "$1/export/." "$2/"\n')
        self.shared_sha = self.commit(self.cloud)
        runtime.command(["git", "clone", "-q", str(self.cloud), str(self.shared)])
        runtime.command(["git", "remote", "set-url", "origin", runtime.SERVER_URL], cwd=self.shared)
        (self.cloud / "server-only.rs").write_text("server fix\n")
        self.source_sha = self.commit(self.cloud)

    def commit(self, repository):
        runtime.command(["git", "add", "-A"], cwd=repository)
        runtime.command(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-qm", "fixture"], cwd=repository)
        return runtime.command(["git", "rev-parse", "HEAD"], cwd=repository)

    def verify(self):
        return runtime.equivalent_server_source(self.cloud, self.source_sha, self.shared, self.shared_sha)

    def test_server_only_change_has_identical_complete_shared_exports(self):
        proof = self.verify()
        self.assertRegex(proof["equivalent_shared_source_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(proof["sync_script_sha256"], r"^[a-f0-9]{64}$")
        desktop = self.root / "desktop"
        write_pins(desktop, self.shared_sha)
        with self.assertRaisesRegex(ValueError, "immutable Cloud revision"):
            runtime.desktop_pin(desktop, self.source_sha)
        self.assertEqual(runtime.desktop_pin(desktop, self.source_sha,
                         allow_equivalent_shared_source=True)["shared_source_sha"], self.shared_sha)

    def test_opt_in_request_records_proof_and_still_requires_successful_image_build(self):
        desktop = self.root / "desktop"
        write_pins(desktop, self.shared_sha)
        runtime.command(["git", "init", "-q", str(desktop)])
        desktop_sha = self.commit(desktop)
        metadata = {**build_metadata(), "display_title": f"CI images 900-1 ({self.source_sha})"}
        manifest = self.root / "manifest.json"

        def run(args, **kwargs):
            if args[0] == "gh":
                return json.dumps(artifacts() if "artifacts?" in args[-1] else metadata)
            return runtime.command(args, **kwargs)

        ci_sha = runtime.command(["git", "rev-parse", "HEAD"], cwd=ROOT)
        with patch.dict(os.environ, {"GITHUB_SHA": ci_sha}):
            self.assertTrue(runtime.prepare_request(self.source_sha, "123", desktop, self.cloud,
                            desktop_sha, manifest, run, shared_cloud=self.shared,
                            allow_equivalent_shared_source=True))
            state = json.loads(manifest.read_text())
            self.assertEqual(state["shared_source_sha"], self.shared_sha)
            self.assertEqual(state["cloud_source_sha"], self.source_sha)
            self.assertIn("equivalent_shared_source_sha256", state)
            metadata["conclusion"] = "cancelled"
            manifest.unlink()
            with self.assertRaisesRegex(ValueError, "successful private integration-image"):
                runtime.prepare_request(self.source_sha, "123", desktop, self.cloud,
                            desktop_sha, manifest, run, shared_cloud=self.shared,
                            allow_equivalent_shared_source=True)
            self.assertFalse(manifest.exists())

        output = self.root / "outputs"
        arguments = [sys.executable, "-B", str(ROOT / ".github/scripts/desktop_cloud_runtime.py"),
                     "pins", "--desktop", str(desktop), "--desktop-ref", desktop_sha,
                     "--cloud-source-sha", self.source_sha, "--image-run-id", "123"]
        environment = {**os.environ, "GITHUB_OUTPUT": str(output)}
        default = subprocess.run(arguments, env=environment, capture_output=True)
        self.assertNotEqual(default.returncode, 0)
        subprocess.run(arguments + ["--allow-equivalent-shared-source"], env=environment,
                       check=True, capture_output=True)
        self.assertEqual(output.read_text(), f"client-sha=\nshared-cloud-sha={self.shared_sha}\n")

    def test_changed_source_recipe_or_executable_mode_is_rejected(self):
        original = self.source_sha
        for mutation in ("source", "recipe", "mode"):
            with self.subTest(mutation=mutation):
                runtime.command(["git", "reset", "--hard", original], cwd=self.cloud)
                if mutation == "recipe":
                    with (self.cloud / "scripts/sync_client_repo.sh").open("a") as handle:
                        handle.write("# changed recipe\n")
                elif mutation == "mode":
                    (self.cloud / "export/libs/client-api/lib.rs").chmod(0o755)
                else:
                    (self.cloud / "export/libs/client-api/lib.rs").write_text("changed protocol\n")
                self.source_sha = self.commit(self.cloud)
                with self.assertRaisesRegex(ValueError, "Pinned shared source differs"):
                    self.verify()

    def test_incomplete_exports_wrong_origin_and_missing_checkout_are_rejected(self):
        (self.cloud / "export/libs/collab/Cargo.toml").unlink()
        self.source_sha = self.commit(self.cloud)
        with self.assertRaisesRegex(ValueError, "workspace is incomplete"):
            self.verify()
        runtime.command(["git", "remote", "set-url", "origin", "https://github.com/other/server"], cwd=self.shared)
        with self.assertRaisesRegex(ValueError, "differs from the Desktop pin"):
            self.verify()
        with self.assertRaisesRegex(ValueError, "differs from the Desktop pin"):
            runtime.equivalent_server_source(self.cloud, self.source_sha, None, self.shared_sha)


if __name__ == "__main__":
    unittest.main()
