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


def archive_image(root, service, *, source=SOURCE, architecture="amd64", tag=None, duplicate=False,
                  oci=False, corrupt=""):
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
    image_id = "sha256:" + digest
    if oci:
        layer = b"test compressed layer"
        layer_digest = hashlib.sha256(layer).hexdigest()
        layer_path = f"blobs/sha256/{layer_digest}"
        docker = json.loads(entries["manifest.json"])
        docker[0]["Layers"] = [layer_path]
        entries["manifest.json"] = json.dumps(docker).encode()
        entries[layer_path] = layer
        manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"digest": image_id, "size": len(encoded)},
                    "layers": [{"digest": "sha256:" + layer_digest, "size": len(layer)}]}
        if corrupt == "config":
            manifest["config"]["digest"] = "sha256:" + "e" * 64
        if corrupt == "layers":
            manifest["layers"][0]["digest"] = "sha256:" + "f" * 64
        if corrupt == "layer-size":
            manifest["layers"][0]["size"] += 1
        contents = json.dumps(manifest).encode()
        manifest_digest = hashlib.sha256(contents).hexdigest()
        image_id = "sha256:" + manifest_digest
        entries[f"blobs/sha256/{manifest_digest}"] = contents + (b" " if corrupt == "digest" else b"")
        descriptor = {"mediaType": manifest["mediaType"], "digest": image_id,
                      "size": len(entries[f"blobs/sha256/{manifest_digest}"]),
                      "platform": {"architecture": "amd64", "os": "linux"}}
        if corrupt == "platform":
            descriptor["platform"]["architecture"] = "arm64"
        if corrupt == "manifest-size":
            descriptor["size"] += 1
        entries["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode()
    path = root / f"{service}.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, contents in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
        if duplicate:
            archive.addfile(member, io.BytesIO(contents))
    return image_id


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

    def test_web_caller_pins_tools_independently_and_requires_the_ai_artifact(self):
        self.artifacts["artifacts"].append({"name": "docker-image-ai", "id": 5, "expired": False})
        with patch.dict(os.environ, {"GITHUB_SHA": "e" * 40}):
            self.assertTrue(runtime.prepare_request(
                SOURCE, "123", self.desktop, self.cloud, DESKTOP, self.manifest,
                self.run_command, ci_ref=CI, profile="web"))
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["ci_source_sha"], CI)
        self.assertEqual(state["caller_source_sha"], "e" * 40)
        self.assertEqual(state["runtime_profile"], "web")
        self.assertEqual(state["image_artifacts"]["docker-image-ai"], 5)
        self.artifacts = artifacts()
        with self.assertRaisesRegex(ValueError, "complete, unexpired"):
            runtime.prepare_request(SOURCE, "123", self.desktop, self.cloud, DESKTOP,
                                    self.manifest, self.run_command, ci_ref=CI, profile="web")

    def test_explicit_tools_ref_cannot_be_mutable_or_differ_from_checkout(self):
        for revision in ("main", "f" * 40):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "CI runtime"):
                runtime.prepare_request(SOURCE, "123", self.desktop, self.cloud, DESKTOP,
                                        self.manifest, self.run_command, ci_ref=revision)
        self.assertTrue(all(call[0] == "git" for call in self.calls))

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


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.desktop = Path(self.directory.name)
        write_pins(self.desktop)
        self.calls = []
        self.artifacts = artifacts()
        self.artifacts["artifacts"].append({"name": "docker-image-ai", "id": 5, "expired": False})
        self.expired = json.loads(json.dumps(self.artifacts))
        self.expired["artifacts"][0]["expired"] = True
        self.sync_message = f"sync: update client-api from {SOURCE[:7]}"
        self.env = patch.dict(os.environ, {"GITHUB_SHA": CI})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "git":
            return CI
        endpoint = args[-1]
        if "/git/commits/" in endpoint:
            return json.dumps({"message": self.sync_message})
        if "/commits/" in endpoint:
            return json.dumps({"sha": SOURCE})
        if "/workflows/" in endpoint:
            return json.dumps({"workflow_runs": [
                {"id": 999, "display_title": f"CI images 9-1 ({'f' * 40})"},
                {"id": 122, "display_title": f"CI images 9-1 ({SOURCE})"},
                {"id": 123, "display_title": f"CI images 9-1 ({SOURCE})"},
            ]})
        if "/artifacts?" in endpoint:
            return json.dumps(self.expired if "/122/" in endpoint else self.artifacts)
        return json.dumps(build_metadata())

    def test_auto_selection_skips_wrong_sources_and_expired_builds_without_dispatch(self):
        self.assertEqual(runtime.select_prebuilt(self.desktop, run=self.run_command), (SOURCE, "123"))
        self.assertFalse(any("/runs/999" in args[-1] for args in self.calls))
        self.assertTrue(all(args[0] == "git" or args[:2] == ["gh", "api"] for args in self.calls))

    def test_generated_client_hint_is_resolved_to_a_full_server_candidate(self):
        write_pins(self.desktop, "f" * 40, repository=runtime.CLIENT_URL)
        self.assertEqual(runtime.select_prebuilt(self.desktop, run=self.run_command), (SOURCE, "123"))
        self.assertTrue(any("AppFlowy-Client/git/commits/" in args[-1] for args in self.calls))
        self.assertTrue(any(f"/commits/{SOURCE[:7]}" in args[-1] for args in self.calls))

    def test_missing_source_hint_and_expired_sets_fail_without_registry_fallback(self):
        write_pins(self.desktop, "f" * 40, repository=runtime.CLIENT_URL)
        self.sync_message = "unrelated commit"
        with self.assertRaisesRegex(ValueError, "canonical Server source hint"):
            runtime.select_prebuilt(self.desktop, run=self.run_command)
        write_pins(self.desktop)
        self.artifacts = self.expired
        with self.assertRaisesRegex(ValueError, "latest 100 builds"):
            runtime.select_prebuilt(self.desktop, run=self.run_command)

    def test_explicit_registry_selection_does_not_require_or_read_private_builds(self):
        self.assertIsNone(runtime.select_prebuilt(self.desktop, cloud_version="published-candidate",
                                                 run=self.run_command))
        self.assertEqual(self.calls, [])

    def test_partial_pairs_conflicting_modes_and_mutable_tools_ref_are_rejected(self):
        for arguments in ({"source_sha": SOURCE}, {"image_run_id": "123"},
                          {"source_sha": SOURCE, "image_run_id": "123", "cloud_version": "latest"},
                          {"ci_ref": "main"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                runtime.select_prebuilt(self.desktop, run=self.run_command, **arguments)


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

    def test_standalone_prost_formatting_is_canonicalized_but_field_changes_are_rejected(self):
        generated = "libs/appflowy-proto/src/pb/collab.rs"
        source = ("// This file is @generated by prost-build.\n"
                  "pub struct Row {\n  pub row_id: u64,\n}\n")
        for directory in (self.cloud / "export", self.client):
            (directory / "rustfmt.toml").write_text('edition = "2024"\ntab_spaces = 2\n')
            file = directory / generated
            file.parent.mkdir(parents=True)
            file.write_text(source)
        self.source = self.commit(self.cloud)
        (self.client / generated).write_text(source.replace("  pub row_id", "    pub row_id"))
        state = self.verify(self.commit(self.client))
        self.assertEqual(state["normalized_prost_outputs"], [generated])
        # The formatter operates on disposable archives, never canonical checkouts.
        self.assertIn("    pub row_id", (self.client / generated).read_text())
        (self.client / generated).write_text(source.replace("u64", "u32"))
        with self.assertRaisesRegex(ValueError, "source differs"):
            self.verify(self.commit(self.client))

    def test_prost_path_marker_and_original_mode_cannot_be_hidden_by_formatting(self):
        generated = "libs/appflowy-proto/src/pb/messages.rs"
        for directory in (self.cloud / "export", self.client):
            (directory / "rustfmt.toml").write_text('edition = "2024"\n')
            file = directory / generated
            file.parent.mkdir(parents=True)
            file.write_text("pub struct Handwritten;\n")
        self.source = self.commit(self.cloud)
        with self.assertRaisesRegex(ValueError, "generation marker"):
            self.verify(self.commit(self.client))
        (self.client / generated).chmod(0o755)
        with self.assertRaisesRegex(ValueError, "source differs"):
            self.verify(self.commit(self.client))

    def test_pin_cli_only_requests_client_checkout_for_generated_exact_mode(self):
        output = self.root / "outputs"
        arguments = [sys.executable, "-B", str(ROOT / ".github/scripts/desktop_cloud_runtime.py"),
                     "pins", "--desktop", str(self.desktop), "--desktop-ref", self.desktop_sha]
        environment = {**os.environ, "GITHUB_OUTPUT": str(output)}
        exact = ["--cloud-source-sha", self.source, "--image-run-id", "123"]
        subprocess.run(arguments + exact, env=environment, check=True, capture_output=True)
        self.assertEqual(output.read_text(), f"client-sha={self.client_sha}\n")
        output.unlink()
        write_pins(self.desktop, self.source)
        arguments[-1] = self.commit(self.desktop)
        subprocess.run(arguments + exact, env=environment, check=True, capture_output=True)
        self.assertEqual(output.read_text(), "client-sha=\n")
        output.unlink()
        # Legacy mode does not inspect source pins or require even a checkout.
        shutil.rmtree(self.desktop)
        subprocess.run(arguments, env=environment, check=True, capture_output=True)
        self.assertEqual(output.read_text(), "client-sha=\n")


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
            "appflowy_gateway": {"image": "appflowyinc/appflowy_cloud_gateway:latest-amd64"},
            "postgres": {"image": "postgres:16"}, "nginx": {"image": "nginx"},
            "inactive": {"image": "example", "profiles": ["opt-in"]},
        }}
        self.wrong_container = False
        self.wrong_loaded_image = False
        self.fail_load = False
        self.timeline_image = "appflowyinc/timeline-ci:published-amd64"
        self.timeline_id = "sha256:prebuilt-commercial-image"
        self.timeline_source = SOURCE
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
            if args[-1] == self.timeline_image:
                return json.dumps([{"Id": self.timeline_id, "Os": "linux", "Architecture": "amd64",
                                    "Config": {"Labels": {"org.opencontainers.image.revision": self.timeline_source}}}])
            service = next(name for name in runtime.KNOWN_IMAGES if args[-1] == runtime.image_tag(name))
            image_id = self.images[service]
            if self.wrong_loaded_image:
                image_id = "sha256:unexpected-loaded-image"
            return json.dumps([{"Id": image_id, "Os": "linux", "Architecture": "amd64",
                                "Config": {"Labels": {"org.opencontainers.image.revision": SOURCE}}}])
        if "ps" in args:
            return args[-1]
        if args[:2] == ["docker", "inspect"]:
            pulled_timeline = any(call == ["docker", "pull", self.timeline_image] for call, _ in self.calls)
            role = "appflowy_ai" if args[-1] == "ai" else args[-1]
            image_id = self.timeline_id if args[-1] == "appflowy_cloud" and pulled_timeline else self.images[role]
            return json.dumps([{"Image": "wrong" if self.wrong_container else image_id}])
        return ""

    def start(self, timeline=False):
        runtime.start_runtime(self.root, self.root, self.manifest, timeline, self.run_command,
                              timeline_image=self.timeline_image if timeline else "")

    def test_real_archives_dependency_only_pull_and_actual_container_ids(self):
        self.start()
        pulls = [args for args, _ in self.calls if "pull" in args]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0][pulls[0].index("pull") + 1:],
                         ["--quiet", "appflowy_gateway", "nginx", "postgres"])
        up = next(args for args, _ in self.calls if "up" in args)
        self.assertEqual(up[-5:], ["up", "-d", "--no-build", "--pull", "never"])
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["stage"], "runtime_verified")
        self.assertEqual(state["images"], self.images)

    def test_containerd_manifest_ids_are_bound_to_verified_config_and_pinned(self):
        self.add_ai_artifact()
        self.images = {name: archive_image(self.root, name, oci=True) for name in self.images}
        self.start()
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["images"], self.images)
        self.assertEqual(state["runtime_images"]["ai"], self.images["appflowy_ai"])
        override = json.loads(self.manifest.with_suffix(".compose.json").read_text())
        self.assertEqual(override["services"]["ai"]["image"], self.images["appflowy_ai"])
        self.assertTrue(all(len(ids) == 2 for ids in state["archive_image_ids"].values()))

    def test_oci_archive_also_accepts_legacy_config_id(self):
        expected = self.images.copy()
        for name in self.images:
            archive_image(self.root, name, oci=True)
        self.start()
        self.assertEqual(json.loads(self.manifest.read_text())["images"], expected)

    def test_oci_metadata_cannot_substitute_config_layers_or_manifest(self):
        for corruption in ("config", "layers", "layer-size", "digest", "platform", "manifest-size"):
            with self.subTest(corruption=corruption), self.assertRaisesRegex(ValueError, "OCI archive"):
                archive_image(self.root, runtime.SERVICES[-1], oci=True, corrupt=corruption)
                self.start()
        self.assertTrue(all(args[0] == "git" for args, _ in self.calls))

    def add_ai_artifact(self, profile="desktop"):
        self.images["appflowy_ai"] = archive_image(self.root, "appflowy_ai")
        self.config["services"]["ai"] = {"image": runtime.image_tag("appflowy_ai")}
        state = json.loads(self.manifest.read_text())
        state.update(runtime_profile=profile, image_artifacts={
            f"docker-image-{name.removeprefix('appflowy_')}": index
            for index, name in enumerate(runtime.KNOWN_IMAGES, 1)
        })
        runtime.write_manifest(self.manifest, state)

    def test_optional_ai_is_verified_and_pinned_in_desktop_runtime(self):
        self.add_ai_artifact()
        self.start()
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["runtime_images"]["ai"], self.images["appflowy_ai"])
        pulls = [args for args, _ in self.calls if "pull" in args]
        self.assertFalse(any("ai" in args for args in pulls))

    def test_wrong_optional_ai_source_refuses_before_any_docker_command(self):
        self.add_ai_artifact()
        archive_image(self.root, "appflowy_ai", source="e" * 40)
        with self.assertRaisesRegex(ValueError, "source revision"):
            self.start()
        self.assertFalse(any(args[0] == "docker" for args, _ in self.calls))

    def test_web_profile_uses_web_compose_and_pins_the_ai_container(self):
        self.add_ai_artifact("web")
        self.config["services"].pop("appflowy_mcp")
        override = self.root / "web-override.yml"
        override.write_text("services: {}\n")
        runtime.start_runtime(self.root, self.root, self.manifest, run=self.run_command,
                              compose_override=override)
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["runtime_images"]["ai"], self.images["appflowy_ai"])
        self.assertNotIn("appflowy_mcp", state["runtime_images"])
        up = next(args for args, _ in self.calls if "up" in args)
        self.assertIn(str(self.root / "docker-compose-web-ci.yml"), up)
        self.assertIn(str(override.resolve()), up)
        self.assertEqual(up[-5:], ["up", "-d", "--no-build", "--pull", "never"])
        self.assertFalse(any("ai" in args for args, _ in self.calls if "pull" in args))

    def test_web_profile_requires_its_override_and_trusted_ai_compose_role(self):
        self.add_ai_artifact("web")
        with self.assertRaisesRegex(ValueError, "Compose override"):
            self.start()
        override = self.root / "web-override.yml"
        override.write_text("services: {}\n")
        self.config["services"]["ai"]["image"] = "untrusted/image:latest"
        with self.assertRaisesRegex(ValueError, "verified artifact for ai"):
            runtime.start_runtime(self.root, self.root, self.manifest, run=self.run_command,
                                  compose_override=override)
        self.assertFalse(any(args[:2] == ["docker", "load"] for args, _ in self.calls))

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

    def test_timeline_pulls_same_source_prebuilt_image_and_pins_its_container_id(self):
        bypass = self.root / "nginx/api-limits-self-host-ci.conf"
        bypass.parent.mkdir()
        bypass.write_text("# self-host policy\n")
        self.start(timeline=True)
        for args, kwargs in self.calls:
            if args[:2] == ["docker", "compose"]:
                self.assertEqual(kwargs["env"]["APPFLOWY_CI_API_LIMITS_NGINX_CONFIG"], str(bypass))
        override = json.loads(self.manifest.with_suffix(".compose.json").read_text())
        self.assertEqual(override["services"]["appflowy_cloud"]["image"], self.timeline_id)
        self.assertTrue(any(args == ["docker", "pull", self.timeline_image] for args, _ in self.calls))
        self.assertFalse(any("build" in args for args, _ in self.calls))
        self.assertTrue(all(value["pull_policy"] == "never" for value in override["services"].values()))
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state["images"]["appflowy_cloud"], self.timeline_id)
        self.assertEqual(state["timeline_image"], self.timeline_image)

    def test_timeline_missing_image_fails_before_any_command(self):
        with self.assertRaisesRegex(ValueError, "prebuilt"):
            runtime.start_runtime(self.root, self.root, self.manifest, True, self.run_command)
        self.assertEqual(self.calls, [])

    def test_timeline_wrong_source_refuses_start_without_build_fallback(self):
        self.timeline_source = "f" * 40
        with self.assertRaisesRegex(ValueError, "source revision"):
            self.start(timeline=True)
        self.assertFalse(any("up" in args or "build" in args for args, _ in self.calls))

    def test_profile_dependencies_are_selected_without_protected_images(self):
        self.assertEqual(runtime.dependency_services(self.config, ["opt-in"]),
                         ["appflowy_gateway", "inactive", "nginx", "postgres"])
        self.assertEqual(runtime.dependency_services(self.config, ["*"]),
                         ["appflowy_gateway", "inactive", "nginx", "postgres"])


class WorkflowWiringTest(unittest.TestCase):
    def workflow(self, name):
        return yaml.safe_load((ROOT / f".github/workflows/{name}.yaml").read_text())

    def test_receiver_forwards_prebuilt_images_without_requesting_a_build(self):
        job = self.workflow("webhook_receiver")["jobs"]["dispatch-workflows"]
        steps = job["steps"]
        self.assertFalse(any("request_image_build.py" in step.get("run", "") for step in steps))
        self.assertFalse(any(step.get("uses", "").startswith("actions/checkout@") for step in steps))
        self.assertLessEqual(job["timeout-minutes"], 20)
        self.assertEqual(job["outputs"]["cloud_image_run_id"], "${{ steps.parse.outputs.cloud_image_run_id }}")
        inputs = self.workflow("webhook_receiver")[True]["workflow_dispatch"]["inputs"]
        self.assertIn("cloud_image_run_id", inputs)
        result, outputs = self.parse_receiver(CLOUD_SOURCE_SHA=SOURCE, CLOUD_IMAGE_RUN_ID="123456789")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("Trigger Flutter CI", "Trigger Rust CI"):
            step = next(step for step in steps if step.get("name") == name)
            payload_text = step["with"]["client-payload"]
            for key, value in outputs.items():
                payload_text = payload_text.replace("${{ steps.parse.outputs." + key + " }}", value)
            payload = json.loads(payload_text)
            self.assertEqual(payload["cloud_source_sha"], SOURCE)
            self.assertEqual(payload["cloud_image_run_id"], "123456789")
            self.assertEqual(payload["pr_ref"], DESKTOP)
            self.assertIn('"cloud_source_sha"', step["with"]["client-payload"])
            self.assertIn('"cloud_image_run_id"', step["with"]["client-payload"])
        ios = next(step for step in steps if step.get("name") == "Trigger iOS CI")
        self.assertNotIn("cloud_image_run_id", ios["with"]["client-payload"])

    def parse_receiver(self, **overrides):
        """Execute the production YAML parser without GitHub or image builders."""
        steps = self.workflow("webhook_receiver")["jobs"]["dispatch-workflows"]["steps"]
        step = next(step for step in steps if step.get("id") == "parse")
        # Payloads must enter the shell as data, not workflow-expression code.
        self.assertNotIn("${{", step["run"])
        env = {
            "PATH": os.environ["PATH"],
            "EVENT_ACTION": "private-repo-webhook",
            "REQUEST_SOURCE_REPO": "AppFlowy-Premium",
            "EVENT_TYPE": "pr_sync",
            "PR_NUMBER": "42",
            "PR_REF": DESKTOP,
            "WORKFLOWS": "flutter,rust",
            "RECEIVER_RUN_ID": "900",
            "CLOUD_SOURCE_SHA": "",
            "CLOUD_IMAGE_RUN_ID": "",
            **overrides,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                env={**env, "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True, timeout=10,
            )
            outputs = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
        return result, outputs

    def test_receiver_defaults_to_registry_images(self):
        for action in ("private-repo-webhook", ""):
            with self.subTest(action=action):
                result, outputs = self.parse_receiver(EVENT_ACTION=action, PR_REF="main")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(outputs["source_repo"], "AppFlowy-Premium")
                self.assertEqual(outputs["pr_ref"], "main")
                self.assertEqual(outputs["cloud_source_sha"], "")
                self.assertEqual(outputs["cloud_image_run_id"], "")

    def test_receiver_normalizes_legacy_desktop_source_only_to_registry(self):
        for ref in (DESKTOP, "main"):
            with self.subTest(ref=ref):
                result, outputs = self.parse_receiver(CLOUD_SOURCE_SHA=SOURCE, PR_REF=ref)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(outputs["cloud_source_sha"], "")
                self.assertEqual(outputs["cloud_image_run_id"], "")
                self.assertEqual(outputs["pr_ref"], ref)
                self.assertIn("::notice::", result.stdout)
                self.assertIn("using registry images", result.stdout)
                self.assertIn("No image build will be requested", result.stdout)

    def test_receiver_preserves_explicit_pair_for_each_desktop_runtime_lane(self):
        for action in ("private-repo-webhook", ""):
            for lanes in ("rust", "flutter", "flutter,rust,ios"):
                with self.subTest(action=action, lanes=lanes):
                    result, outputs = self.parse_receiver(
                        EVENT_ACTION=action, WORKFLOWS=lanes,
                        CLOUD_SOURCE_SHA=SOURCE, CLOUD_IMAGE_RUN_ID="123456789",
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(outputs["cloud_source_sha"], SOURCE)
                    self.assertEqual(outputs["cloud_image_run_id"], "123456789")
                    self.assertEqual(outputs["workflows"], lanes)

    def test_receiver_rejects_incomplete_and_malformed_image_requests(self):
        cases = [
            {"CLOUD_IMAGE_RUN_ID": "123"},
            *({"CLOUD_SOURCE_SHA": value} for value in ("main", "A" * 40, SOURCE + "\n")),
            *({"CLOUD_SOURCE_SHA": SOURCE, "CLOUD_IMAGE_RUN_ID": value}
              for value in ("0", "01", "-1", "12x", "123\n")),
        ]
        for request in cases:
            with self.subTest(request=request):
                result, outputs = self.parse_receiver(**request)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::", result.stdout)
                self.assertEqual(outputs, {})

    def test_exact_pair_requires_desktop_source_immutable_ref_and_runtime_lane(self):
        cases = [
            {"EVENT_ACTION": "cloud-premium-webhook"},
            {"EVENT_ACTION": "", "REQUEST_SOURCE_REPO": "AppFlowy-Cloud-Premium"},
            {"EVENT_ACTION": "", "REQUEST_SOURCE_REPO": "other"},
            {"PR_REF": "main"},
            {"PR_REF": "refs/pull/42/head"},
            *({"WORKFLOWS": value} for value in ("ios", "rustlint", "rust_coverage", "flutter-extra", "")),
        ]
        for request in cases:
            with self.subTest(request=request):
                result, outputs = self.parse_receiver(
                    CLOUD_SOURCE_SHA=SOURCE, CLOUD_IMAGE_RUN_ID="123", **request,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::", result.stdout)
                self.assertEqual(outputs, {})

    def test_receiver_keeps_standard_server_dispatch_metadata(self):
        for action in ("cloud-premium-webhook", ""):
            with self.subTest(action=action):
                result, outputs = self.parse_receiver(
                    EVENT_ACTION=action, REQUEST_SOURCE_REPO="AppFlowy-Cloud-Premium",
                    EVENT_TYPE="push", PR_NUMBER="", PR_REF="main", WORKFLOWS="backend,integration",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(outputs["source_repo"], "AppFlowy-Cloud-Premium")
                self.assertEqual(outputs["event_type"], "push")
                self.assertEqual(outputs["pr_number"], "")
                self.assertEqual(outputs["pr_ref"], "main")
                self.assertEqual(outputs["workflows"], "backend,integration")
                self.assertEqual(outputs["cloud_source_sha"], "")
                self.assertEqual(outputs["cloud_image_run_id"], "")
        # Source-only normalization is for old Desktop senders, not Server requests.
        result, _ = self.parse_receiver(EVENT_ACTION="cloud-premium-webhook", CLOUD_SOURCE_SHA=SOURCE)
        self.assertNotEqual(result.returncode, 0)

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
            legacy = next(step for step in steps if "published registry" in step.get("name", ""))
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

    def test_generated_client_checkout_is_conditional_canonical_and_precedes_verification(self):
        action = yaml.safe_load((ROOT / ".github/actions/desktop-cloud-runtime/action.yml").read_text())
        steps = action["runs"]["steps"]
        checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
        self.assertEqual(checkout["if"], "steps.pins.outputs.client-sha != ''")
        self.assertEqual(checkout["with"]["repository"], "AppFlowy-IO/AppFlowy-Client")
        self.assertEqual(checkout["with"]["ref"], "${{ steps.pins.outputs.client-sha }}")
        self.assertFalse(checkout["with"]["persist-credentials"])
        request = next(step for step in steps if step.get("id") == "request")
        self.assertLess(steps.index(checkout), steps.index(request))
        self.assertIn('--client "$GITHUB_WORKSPACE/.ci-generated-client"', request["run"])


if __name__ == "__main__":
    unittest.main()
