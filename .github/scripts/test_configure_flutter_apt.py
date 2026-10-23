"""Exercise APT configuration against disposable files, without root or network."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from configure_flutter_apt import configure_apt


class FlutterAptConfigurationTest(unittest.TestCase):
    def test_runner_mirrorlist_uses_primary_archive_without_changing_sources(self):
        # GitHub's Ubuntu images select mirrors through this file, rather than
        # embedding the Azure archive URL in sources.list or ubuntu.sources.
        mirrorlist = (
            "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
            "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
            "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
        )
        source_layouts = {
            "sources.list": (
                "deb mirror+file:/etc/apt/apt-mirrors.txt noble main universe\n"
                "deb mirror+file:/etc/apt/apt-mirrors.txt noble-security main\n"
            ),
            "sources.list.d/ubuntu.sources": (
                "Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\n"
                "Suites: noble noble-updates noble-backports noble-security\n"
                "Components: main universe restricted multiverse\n"
                "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
            ),
        }
        for source_name, contents in source_layouts.items():
            with self.subTest(source_name=source_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / source_name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(contents)
                mirrors = root / "apt-mirrors.txt"
                mirrors.write_text(mirrorlist)
                backup = root / "apt-mirrors.txt.bak"
                backup.write_text(mirrorlist)

                self.assertEqual(configure_apt(root), 1)

                self.assertEqual(source.read_text(), contents)
                self.assertEqual(
                    mirrors.read_text(),
                    mirrorlist.replace(
                        "http://azure.archive.ubuntu.com/",
                        "https://archive.ubuntu.com/",
                    ),
                )
                self.assertEqual(backup.read_text(), mirrorlist)
                self.assertEqual(configure_apt(root), 0)

    def test_legacy_and_deb822_sources_preserve_security_and_vendor_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parts = root / "sources.list.d"
            parts.mkdir()
            sources = {
                root / "sources.list": (
                    "deb http://azure.archive.ubuntu.com/ubuntu noble main universe\n"
                    "deb http://security.ubuntu.com/ubuntu noble-security main\n"
                ),
                parts / "ubuntu.sources": (
                    "Types: deb\nURIs: https://azure.archive.ubuntu.com/ubuntu/\n"
                    "Suites: noble noble-updates noble-backports\n"
                    "Components: main universe restricted multiverse\n"
                    "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
                ),
                parts / "vendor.list": (
                    "deb https://storage.googleapis.com/download.dartlang.org/linux/debian stable main\n"
                ),
                parts / "custom.sources": (
                    "URIs: https://azure.archive.ubuntu.com.example/ubuntu\n"
                ),
                parts / "disabled.sources.bak": (
                    "URIs: http://azure.archive.ubuntu.com/ubuntu\n"
                ),
            }
            for path, contents in sources.items():
                path.write_text(contents)

            self.assertEqual(configure_apt(root), 2)
            for path, contents in sources.items():
                expected = contents
                if path.name in ("sources.list", "ubuntu.sources"):
                    expected = contents.replace(
                        "http://azure.archive.ubuntu.com/", "https://archive.ubuntu.com/"
                    ).replace(
                        "https://azure.archive.ubuntu.com/", "https://archive.ubuntu.com/"
                    )
                self.assertEqual(path.read_text(), expected)
            self.assertEqual(configure_apt(root), 0)

    def test_network_limits_are_applied_even_when_primary_archive_is_already_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "apt.conf.d"
            config.mkdir()
            existing = config / "20existing"
            existing.write_text('Acquire::Languages "none";\n')
            self.assertEqual(configure_apt(root), 0)
            limits = (config / "99zz-appflowy-network").read_text()
            for option in (
                'Acquire::Retries "1";',
                'Acquire::http::Timeout "20";',
                'Acquire::https::Timeout "20";',
            ):
                self.assertIn(option, limits)
            self.assertEqual(existing.read_text(), 'Acquire::Languages "none";\n')

    def test_command_refuses_to_change_a_local_development_environment(self):
        script = Path(__file__).with_name("configure_flutter_apt.py")
        for github_actions, runner_os in (("false", "Linux"), ("true", "macOS")):
            with self.subTest(github_actions=github_actions, runner_os=runner_os):
                result = subprocess.run(
                    ["python3", "-B", str(script)],
                    env={**os.environ, "GITHUB_ACTIONS": github_actions, "RUNNER_OS": runner_os},
                    capture_output=True, text=True, timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only for GitHub Linux runners", result.stderr)
