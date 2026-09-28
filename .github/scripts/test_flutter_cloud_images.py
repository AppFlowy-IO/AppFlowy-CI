"""Regressions for a partially published Cloud/Worker release."""

import copy
from pathlib import Path
import unittest

import yaml

from select_flutter_cloud_images import REPOSITORIES, select_images


OLD_REVISION = "a" * 40
NEW_REVISION = "b" * 40


def image(version="0.18.62", revision=OLD_REVISION, digest="1"):
    return {
        "manifest": {"digest": "sha256:" + digest * 64},
        "image": {"config": {"Labels": {
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.revision": revision,
        }}},
    }


class CloudImageSelectionTest(unittest.TestCase):
    def setUp(self):
        self.registry = {
            f"{repository}:0.18.62-amd64": image(digest=str(index))
            for index, repository in enumerate(REPOSITORIES.values(), 1)
        }
        self.registry[f"{REPOSITORIES['appflowy_cloud']}:latest-amd64"] = image()
        # Cloud failed to build while every other service advanced latest.
        for service in ("appflowy_worker", "appflowy_search", "appflowy_mcp"):
            self.registry[f"{REPOSITORIES[service]}:latest-amd64"] = image(
                "0.19.1", NEW_REVISION, "9",
            )

    def test_partial_release_uses_the_cloud_migrators_matching_workers(self):
        revision, compose = select_images("latest-amd64", self.registry.__getitem__)
        self.assertEqual(revision, OLD_REVISION)
        for index, (service, repository) in enumerate(REPOSITORIES.items(), 1):
            self.assertEqual(
                compose["services"][service]["image"],
                f"{repository}@sha256:{str(index) * 64}",
            )

    def test_same_tag_from_different_source_is_rejected(self):
        self.registry[f"{REPOSITORIES['appflowy_worker']}:0.18.62-amd64"] = image(
            revision=NEW_REVISION,
        )
        with self.assertRaisesRegex(ValueError, "appflowy_worker.*Cloud requires"):
            select_images("latest-amd64", self.registry.__getitem__)

    def test_missing_matching_worker_does_not_fall_back_to_latest(self):
        del self.registry[f"{REPOSITORIES['appflowy_worker']}:0.18.62-amd64"]
        with self.assertRaises(KeyError):
            select_images("latest-amd64", self.registry.__getitem__)

    def test_unverifiable_image_is_rejected(self):
        reference = f"{REPOSITORIES['appflowy_cloud']}:latest-amd64"
        for key in ("version", "revision"):
            with self.subTest(key=key):
                registry = copy.deepcopy(self.registry)
                registry[reference]["image"]["config"]["Labels"][
                    "org.opencontainers.image." + key
                ] = ""
                with self.assertRaises(ValueError):
                    select_images("latest-amd64", registry.__getitem__)

    def test_workflow_checks_out_matching_source_and_uses_pinned_compose_images(self):
        workflow = Path(__file__).resolve().parents[1] / "workflows/flutter_ci.yaml"
        steps = yaml.safe_load(workflow.read_text())["jobs"]["cloud_integration_test"]["steps"]
        checkout = next(step for step in steps if step["name"] == "Checkout AppFlowy-Cloud-Premium code")
        self.assertEqual(checkout["with"]["ref"], "${{ steps.cloud-images.outputs.revision }}")
        compose = next(step["run"] for step in steps if step["name"] == "Run Docker-Compose")
        self.assertIn('-f "$RUNNER_TEMP/cloud-backend-images.json"', compose)
        for operation in ("pull", "build appflowy_cloud", "up -d"):
            self.assertIn('"${compose[@]}" ' + operation, compose)


if __name__ == "__main__":
    unittest.main()
