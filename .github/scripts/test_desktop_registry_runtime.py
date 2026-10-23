"""Execute the published-image startup CLI with an isolated Docker boundary."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("desktop_cloud_runtime.py")


class RegistryRuntimeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.cloud = self.root / "cloud"
        self.cloud.mkdir()
        (self.cloud / "docker-compose-ci.yml").write_text(
            "services:\n  appflowy_cloud:\n    image: registry/cloud:latest\n    build: .\n"
        )
        self.calls = self.root / "docker-calls"
        docker = self.root / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['TEST_DOCKER_CALLS'], 'a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if 'build' in sys.argv: sys.exit(99)\n"
            "if 'pull' in sys.argv and '--pull' not in sys.argv and os.getenv('TEST_PULL_FAIL'): sys.exit(42)\n"
        )
        docker.chmod(0o755)
        self.environment = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}",
                            "TEST_DOCKER_CALLS": str(self.calls)}

    def start(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "registry", "--cloud", str(self.cloud), *arguments],
            env=self.environment, text=True, capture_output=True,
        )

    def commands(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_default_pulls_compose_images_then_starts_with_builds_disabled(self):
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        compose = ["compose", "-f", str(self.cloud / "docker-compose-ci.yml")]
        self.assertEqual(self.commands(), [compose + ["pull", "--quiet"],
                                          compose + ["up", "-d", "--no-build", "--pull", "never"]])
        self.assertFalse((self.cloud / "desktop-timeline-image.compose.json").exists())

    def test_missing_published_image_stops_without_start_or_source_build(self):
        self.environment["TEST_PULL_FAIL"] = "1"
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(self.commands()[0][-2:], ["pull", "--quiet"])

    def test_timeline_uses_same_explicit_prebuilt_override_for_pull_and_start(self):
        image = "registry.example/timeline@sha256:" + "a" * 64
        result = self.start("--timeline", "--timeline-image", image)
        self.assertEqual(result.returncode, 0, result.stderr)
        override = self.cloud / "desktop-timeline-image.compose.json"
        self.assertEqual(json.loads(override.read_text()),
                         {"services": {"appflowy_cloud": {"image": image}}})
        commands = self.commands()
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][:-2], commands[1][:-5])
        self.assertIn(str(override), commands[0])
        self.assertEqual(commands[1][-5:], ["up", "-d", "--no-build", "--pull", "never"])

    def test_timeline_without_prebuilt_image_fails_before_docker(self):
        result = self.start("--timeline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prebuilt self-hosted image", result.stderr)
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
