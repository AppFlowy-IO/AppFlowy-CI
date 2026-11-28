import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import native_contract_plan as plan
import published_cloud_images as images
import private_image_lock as private_lock


class NativeContractPlanTests(unittest.TestCase):
    def test_selection_and_unknown_changes(self):
        root = Path.cwd()
        permission = "script/permission_model_test_runner.py"
        presentation = "tests/presentation_acceptance.rs"
        self.assertEqual(plan.select(["doc/context/ci/README.md"], root), set())
        self.assertEqual(plan.select([permission, presentation], root), {"permission", "presentation"})
        self.assertEqual(plan.select([permission, "Cargo.lock"], root), plan.ALL)
        self.assertEqual(plan.select(["doc/context/ci/config.json"], root), plan.ALL)
        self.assertEqual(plan.select([], root), plan.ALL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "Cargo.lock"
            lock.write_text('[[package]]\nname="appflowy-ai"\n')
            path = ["services/appflowy-ai/src/image/open_ai_image.rs"]
            self.assertEqual(plan.select(path, root), {"presentation"})
            with lock.open("a") as output:
                output.write('[[package]]\nname="consumer"\ndependencies=["appflowy-ai 1.0.0"]\n')
            self.assertEqual(plan.select(path, root), plan.ALL)

    def test_immutable_diff_handles_races_renames_and_truncation(self):
        sha, base = "1" * 40, "2" * 40
        def diff(files, head=sha):
            request = Mock(side_effect=[{"head": {"sha": head}, "base": {"sha": base}},
                                        {"files": files}])
            value = plan.changed_paths(request, sha, "123")
            if head == sha:
                self.assertIn(f"compare/{base}...{sha}", request.call_args.args[0])
            return value
        renamed = {"status": "renamed", "filename": "doc/context/x.md", "previous_filename": "src/lib.rs"}
        self.assertEqual(diff([renamed]), ["doc/context/x.md", "src/lib.rs"])
        self.assertIsNone(diff([renamed], "3" * 40))
        self.assertIsNone(diff([renamed] * 300))
        self.assertIsNone(diff([]))

    def test_manual_runs_legacy_refs_and_unavailable_diffs_keep_all_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outputs"
            env = {"GITHUB_OUTPUT": str(path), "AVAILABLE_PHASES": '["cache","permission"]',
                   "GITHUB_EVENT_NAME": "repository_dispatch", "SOURCE_EVENT": "pr_sync",
                   "SOURCE_PR": "123", "SOURCE_SHA": "1" * 40}
            for event, phases, expected in (("workflow_dispatch", env["AVAILABLE_PHASES"], True),
                                            ("repository_dispatch", '["all"]', False),
                                            ("repository_dispatch", env["AVAILABLE_PHASES"], True)):
                with self.subTest(event=event, phases=phases), patch.dict(os.environ, dict(
                        env, GITHUB_EVENT_NAME=event, AVAILABLE_PHASES=phases)), \
                        patch.object(plan, "changed_paths", side_effect=OSError):
                    path.write_text("")
                    plan.main()
                    outputs = dict(line.split("=", 1) for line in path.read_text().splitlines())
                    self.assertEqual(json.loads(outputs["phases"]), json.loads(phases))
                    self.assertEqual(outputs["share"], str(expected).lower())
                    self.assertEqual(outputs["presentation"], "true")


class PublishedImageTests(unittest.TestCase):
    def test_only_digest_pinned_registry_references_can_be_pulled(self):
        pinned = "docker.io/appflowyinc/fixture:latest@sha256:" + "c" * 64
        def lock(image, policy="never"):
            return {"services": {"fixture": {"image": image, "pull_policy": policy}}}
        self.assertEqual(images.references(lock(pinned), []), [pinned])
        for unsafe in (lock(pinned.replace("docker.io", "registry.invalid")), lock("fixture:latest"),
                       lock(pinned, "always")):
            with self.assertRaises(ValueError):
                images.references(unsafe, [])
        with self.assertRaises(KeyError):
            images.references(lock(pinned), ["missing"])

    def test_credentials_logs_and_config_are_isolated_on_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, RUNNER_TEMP=tmp, REGISTRY_TOKEN="private"):
            for failure in (False, True):
                calls = []
                def run(cmd, **kwargs):
                    calls.append((cmd, kwargs))
                    self.assertNotIn("REGISTRY_TOKEN", kwargs["env"])
                    self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
                    self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
                    self.assertTrue(Path(cmd[2], "config.json").is_file())
                    return subprocess.CompletedProcess(cmd, int(failure and "pull" in cmd))
                with patch.object(images.subprocess, "run", side_effect=run):
                    if failure:
                        with self.assertRaises(RuntimeError):
                            images.pull_images(["pinned-image"], "user", "private")
                    else:
                        images.pull_images(["pinned-image"], "user", "private")
                self.assertEqual([c[0][3] for c in calls], ["login","pull","logout"])
                self.assertEqual(calls[0][1]["input"], "private\n")
                self.assertFalse(Path(calls[0][0][2]).exists())

    def test_only_a_correlated_private_artifact_can_be_selected(self):
        sha = "1" * 40
        run = {"id": 42, "display_title": f"CI images 123-1 ({sha}) [PR #2]", "status": "in_progress"}
        artifact = {"name": "private-image-lock-123-1", "expired": False, "workflow_run": {"id": 42}}
        request = Mock(side_effect=[{"private": True}, {"workflow_runs": [run]}, {"artifacts": [artifact]}])
        self.assertEqual(private_lock.locate(sha,"123",2,"2",request=request), (42, artifact["name"]))
        with self.assertRaisesRegex(RuntimeError, "private producer"):
            private_lock.locate(sha,"123",1,"2",request=Mock(return_value={"private":False}))
        request = Mock(side_effect=[{"private":True},{"workflow_runs":[dict(run,status="completed")]},
                                    {"artifacts":[dict(artifact,workflow_run={"id": 99})]}])
        with self.assertRaisesRegex(RuntimeError, "without its dependency manifest"):
            private_lock.locate(sha,"123",1,"2",request=request)
