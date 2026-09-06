"""Exercise the cloud workflow's certificate setup without changing system trust."""

import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/flutter_ci.yaml"


class CloudHttpsWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = yaml.safe_load(WORKFLOW.read_text())["jobs"]["cloud_integration_test"]

    def step_script(self, name):
        return next(step["run"] for step in self.cloud["steps"] if step.get("name") == name)

    def test_generated_certificate_verifies_localhost_with_the_installed_ca(self):
        with tempfile.TemporaryDirectory(prefix="cloud-ci-https-") as directory:
            root = Path(directory)
            certs = root / "nginx/ssl"
            certs.mkdir(parents=True)
            trust = root / "trust"
            trust.mkdir()
            # Only redirect the privileged trust-store operations. Run the actual
            # workflow's certificate generation, including its temporary-key cleanup.
            script = self.step_script("Trust localhost HTTPS for cloud tests")
            script = script.replace("/usr/local/share/ca-certificates", str(trust))
            script = "sudo() { \"$@\"; }\nupdate-ca-certificates() { :; }\n" + script
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                cwd=root,
                env={**os.environ, "RUNNER_TEMP": directory},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(root.glob("appflowy-ci-tls.*")), [])
            certificate = certs / "certificate.crt"
            key = certs / "private_key.key"
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key)

            for hostname, succeeds in [("localhost", True), ("example.com", False)]:
                with self.subTest(hostname=hostname):
                    verified = subprocess.run(
                        [
                            "openssl", "verify", "-purpose", "sslserver",
                            "-CAfile", str(trust / "appflowy-ci.crt"),
                            "-verify_hostname", hostname, str(certificate),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(verified.returncode == 0, succeeds, verified.stderr)

    def test_health_check_stops_the_job_when_tls_verification_fails(self):
        for curl_exit in [0, 60]:
            with self.subTest(curl_exit=curl_exit), tempfile.TemporaryDirectory() as directory:
                arguments = Path(directory) / "curl-arguments"
                script = (
                    'curl() { printf "%s\\n" "$@" > "$CURL_ARGUMENTS"; return "$CURL_EXIT"; }\n'
                    'sleep() { :; }\n'
                    + self.step_script("Wait for appflowy cloud to be ready")
                )
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script],
                    env={
                        **os.environ,
                        **self.cloud["env"],
                        "CURL_ARGUMENTS": str(arguments),
                        "CURL_EXIT": str(curl_exit),
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode == 0, curl_exit == 0, result.stdout)
                flags = arguments.read_text().splitlines()
                self.assertIn("https://localhost/api/health", flags)
                self.assertNotIn("--insecure", flags)
                self.assertNotIn("-k", flags)


if __name__ == "__main__":
    unittest.main()
