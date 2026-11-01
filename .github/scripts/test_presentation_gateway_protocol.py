"""Cloud's source-owned protocol selects a compatible immutable Service fixture."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import presentation_gateway_protocol as protocol


class ServicePinTest(unittest.TestCase):
    def source(self, directory, mode):
        source = Path(directory) / "cloud"
        runner = source / "script/test_presentation_gateway_contract.py"
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("PRESENTATION_CLOUD_BRIDGE_ADDRESS" if mode == protocol.INGRESS
                          else "legacy signed-accounting bridge")
        return source

    def test_default_pins_and_completion_contract_follow_the_source_protocol(self):
        for mode in (protocol.LEGACY, protocol.INGRESS):
            with self.subTest(mode=mode.service_sha), tempfile.TemporaryDirectory() as directory:
                source = self.source(directory, mode)
                self.assertEqual(protocol.gateway_protocol(source), mode)
                self.assertEqual(protocol.service_revision(source), mode.service_sha)
                self.assertEqual(protocol.service_revision(source, ""), mode.service_sha)
        self.assertNotEqual(protocol.LEGACY.service_sha, protocol.INGRESS.service_sha)
        self.assertNotEqual(protocol.LEGACY.completion_marker, protocol.INGRESS.completion_marker)

    def test_explicit_revision_is_preserved_and_invalid_overrides_fail(self):
        for mode in (protocol.LEGACY, protocol.INGRESS):
            with self.subTest(mode=mode.service_sha), tempfile.TemporaryDirectory() as directory:
                source = self.source(directory, mode)
                self.assertEqual(protocol.service_revision(source, "b" * 40), "b" * 40)
                for override in ("main", "short", "A" * 40, "a" * 40 + "\nINJECTED=value"):
                    with self.subTest(override=override), self.assertRaises(RuntimeError):
                        protocol.service_revision(source, override)
        with self.assertRaises(RuntimeError):
            protocol.service_revision(Path("/missing-cloud"), "b" * 40)

    def test_workflow_cli_exports_resolved_default_and_explicit_override(self):
        for mode in (protocol.LEGACY, protocol.INGRESS):
            for override in ("", "b" * 40):
                with self.subTest(mode=mode.service_sha, override=override), \
                        tempfile.TemporaryDirectory() as directory:
                    source = self.source(directory, mode)
                    output = Path(directory) / "github-env"
                    output.write_text("EXISTING=retained\n")
                    subprocess.run([
                        sys.executable, "-B", str(Path(protocol.__file__)),
                        "--source", str(source), "--github-env", str(output),
                    ], env={**os.environ, "PRESENTATION_SERVICE_SHA": override},
                       capture_output=True, text=True, check=True, timeout=30)
                    expected = override or mode.service_sha
                    self.assertEqual(output.read_text(),
                                     f"EXISTING=retained\nPRESENTATION_SERVICE_SHA={expected}\n")


if __name__ == "__main__":
    unittest.main()
