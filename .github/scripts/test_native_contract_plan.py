import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import native_contract_plan as plan
import published_cloud_images as images


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
    def test_external_owners_and_releases_come_from_compose(self):
        service = lambda tag: {"image": "appflowyinc/" + tag, "environment": {"SECRET": "private"}}
        model = {name: service(name + ":latest-amd64") for name in
                 (*images.SOURCE_SERVICES, "appflowy_gateway", "appflowy_billing", "admin_frontend")}
        model.update(gotrue=service("gotrue:pinned"), gateway_clickhouse={"image": "clickhouse/server:24"},
                     redis={"image": "redis"}, future=service("future:release"))
        external = images.external_services(model)
        self.assertEqual(set(external), {"gotrue", "future", "appflowy_gateway", "appflowy_billing",
                                         "admin_frontend", "gateway_clickhouse"})
        self.assertEqual(external["gotrue"], {"image": "appflowyinc/gotrue:pinned"})
        self.assertTrue(all(set(entry) == {"image"} for entry in external.values()))
        self.assertNotIn("appflowy_gateway", images.external_services({"appflowy_cloud": model["appflowy_cloud"]}))

    def test_lock_contains_only_immutable_image_fields_and_fails_on_missing_registry_images(self):
        source = {"billing": {"image": "appflowyinc/billing:latest"}}
        pinned = source["billing"]["image"] + "@sha256:" + "c" * 64
        with patch.object(subprocess, "check_output", return_value=json.dumps({
                "services": {"billing": {"image": pinned, "networks": {}}}})) as run:
            digests = images.resolve(source)
            self.assertEqual(digests, {"billing": "sha256:" + "c" * 64})
            self.assertNotIn("appflowyinc", json.dumps(digests))
            self.assertEqual(images.compose_override(source, digests), {"services": {"billing": {"image": pinned}}})
            for mismatch in ({}, {"billing": "latest"}):
                with self.assertRaises(ValueError):
                    images.compose_override(source, mismatch)
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"services": source})
        with patch.object(subprocess, "check_output", return_value=json.dumps({"services": source})):
            with self.assertRaises(ValueError):
                images.resolve(source)
        with patch.object(subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, "docker")):
            with self.assertRaises(subprocess.CalledProcessError):
                images.resolve(source)
