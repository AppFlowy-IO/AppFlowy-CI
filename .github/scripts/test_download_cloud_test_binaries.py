"""Exercise the real streaming downloader against interrupted localhost HTTP transfers."""

from contextlib import contextmanager, redirect_stdout
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import cloud_test_binaries as binaries
import download_cloud_test_binaries as download


SHA = "a" * 40
RUN = 123
HOST = ["Linux", "x86_64", "ubuntu", "24.04"]
TOKEN = "fixture-secret-never-log"


def packed_archive(workspace, *, source_sha=SHA):
    manifest = {
        "version": 1, "source_sha": source_sha, "workspace": str(workspace), "host": HOST,
        "features": [], "tests": [{"name": "fixture", "kind": ["test"],
                                    "path": "target/fixture"}], "library_paths": [], "env": {},
    }
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, payload, mode in [
            (str(binaries.MANIFEST), json.dumps(manifest).encode(), 0o644),
            ("target/fixture", b"#!/bin/sh\nexit 0\n", 0o755),
            ("target/fixture-data", os.urandom(128 * 1024), 0o644),
        ]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(payload), mode
            archive.addfile(info, io.BytesIO(payload))
    return result.getvalue()


def zip_payload(payload, name=download.ARCHIVE_NAME):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(name, payload)
    return result.getvalue()


def metadata(payload):
    return {"id": 456, "name": f"cloud-test-binaries-{SHA}", "expired": False,
            "size_in_bytes": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "workflow_run": {"id": RUN}}


@contextmanager
def endpoint(responses):
    """Each API request redirects to a blob which may close before its promised length."""
    requests = []
    blobs = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            if self.path == "/api":
                self.send_response(302)
                self.send_header("Location", "/blob?signature=fixture-signed-query")
                self.end_headers()
                return
            payload, length, status = responses[min(len(blobs), len(responses) - 1)]
            blobs.append(payload)
            self.send_response(status)
            self.send_header("Content-Length", str(length))
            self.end_headers()
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # A bounded client can stop an oversized or timed-out fixture response.
            self.close_connection = True

        def log_message(self, *_arguments):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api", requests, blobs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cloud-download-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "source"
        self.workspace.mkdir()
        self.payload = packed_archive(self.workspace)
        self.zipped = zip_payload(self.payload)
        self.artifact = metadata(self.zipped)
        self.destination = self.root / "download" / download.ARCHIVE_NAME
        self.output = io.StringIO()

    def fetch(self, url, **arguments):
        with redirect_stdout(self.output):
            download.download_artifact(self.artifact, self.destination, TOKEN,
                                       url=url, wait=lambda _seconds: None, **arguments)

    def assert_scratch_removed(self):
        self.assertEqual(list(self.destination.parent.glob(".cloud-test-download-*")), [])
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn("fixture-signed-query", self.output.getvalue())

    def test_interrupted_transfer_is_discarded_then_fresh_download_restores(self):
        with endpoint([(self.zipped[:500], len(self.zipped), 200),
                       (self.zipped, len(self.zipped), 200)]) as (url, requests, blobs):
            self.fetch(url)
        self.assertEqual(len(blobs), 2)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual([auth for path, auth in requests if path == "/api"],
                         [f"Bearer {TOKEN}", f"Bearer {TOKEN}"])
        self.assertTrue(all(auth is None for path, auth in requests if path.startswith("/blob")))
        with patch("os.getcwd", return_value=str(self.workspace)), \
                patch.object(binaries, "host_identity", return_value=HOST), \
                redirect_stdout(self.output):
            binaries.restore(self.destination, SHA)
        self.assertTrue((self.workspace / "target/fixture").exists())
        self.assert_scratch_removed()

    def test_permanent_truncation_exhausts_three_attempts_without_publishing(self):
        with endpoint([(self.zipped[:500], len(self.zipped), 200)]) as (url, _requests, blobs):
            with self.assertRaisesRegex(download.TransferFailure, "exhausted"):
                self.fetch(url)
        self.assertEqual(len(blobs), 3)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.workspace / "target").exists())
        self.assert_scratch_removed()

    def test_short_successful_response_is_retried_even_without_content_length_mismatch(self):
        with endpoint([(self.zipped[:500], 500, 200),
                       (self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            self.fetch(url)
        self.assertEqual(len(blobs), 2)
        self.assertIn("Incomplete artifact", self.output.getvalue())
        self.assert_scratch_removed()

    def test_same_length_digest_mismatch_is_retried(self):
        damaged = bytearray(self.zipped)
        damaged[100] ^= 1
        with endpoint([(bytes(damaged), len(damaged), 200),
                       (self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            self.fetch(url)
        self.assertEqual(len(blobs), 2)
        self.assertIn("SHA256 differs", self.output.getvalue())
        self.assert_scratch_removed()

    def test_attempt_deadline_is_checked_during_a_real_response(self):
        with endpoint([(self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            with patch.object(download.time, "monotonic", side_effect=[0, 11]), \
                    self.assertRaisesRegex(download.TransferFailure, "deadline"):
                download.transfer(url, self.root / "deadline.zip", self.artifact, TOKEN, timeout=10)
        self.assertEqual(len(blobs), 1)
        self.assertFalse(self.destination.exists())

    def test_authorization_failure_is_not_retried(self):
        with endpoint([(b"denied", 6, 403)]) as (url, _requests, blobs):
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                self.fetch(url)
        self.assertEqual(len(blobs), 1)
        self.assertFalse(self.destination.exists())
        self.assert_scratch_removed()

    def test_server_failure_is_retried(self):
        with endpoint([(b"busy", 4, 503),
                       (self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            self.fetch(url)
        self.assertEqual(len(blobs), 2)
        self.assert_scratch_removed()

    def test_valid_download_with_wrong_source_remains_a_hard_restore_failure(self):
        self.zipped = zip_payload(packed_archive(self.workspace, source_sha="b" * 40))
        self.artifact = metadata(self.zipped)
        with endpoint([(self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            self.fetch(url)
            with patch("os.getcwd", return_value=str(self.workspace)), \
                    self.assertRaisesRegex(ValueError, "different commits"):
                binaries.restore(self.destination, SHA)
        self.assertEqual(len(blobs), 1)
        self.assertFalse((self.workspace / "target").exists())
        self.assert_scratch_removed()

    def test_damaged_published_gzip_is_not_retried_or_published(self):
        self.zipped = zip_payload(self.payload[:-10])
        self.artifact = metadata(self.zipped)
        with endpoint([(self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            with self.assertRaises(EOFError):
                self.fetch(url)
        self.assertEqual(len(blobs), 1)
        self.assertFalse(self.destination.exists())
        self.assert_scratch_removed()

    def test_unexpected_zip_members_are_not_extracted(self):
        self.zipped = zip_payload(self.payload, "../escaped.tar.gz")
        self.artifact = metadata(self.zipped)
        with endpoint([(self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            with self.assertRaisesRegex(ValueError, "only cloud-tests"):
                self.fetch(url)
        self.assertEqual(len(blobs), 1)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "escaped.tar.gz").exists())
        self.assert_scratch_removed()

    def test_older_artifact_without_digest_still_requires_zip_and_gzip_integrity(self):
        self.artifact["digest"] = None
        with endpoint([(self.zipped, len(self.zipped), 200)]) as (url, _requests, blobs):
            self.fetch(url)
        self.assertEqual(len(blobs), 1)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assert_scratch_removed()

    def test_missing_digest_does_not_bypass_zip_crc(self):
        damaged = bytearray(self.zipped)
        damaged[100] ^= 1
        self.artifact["digest"] = None
        with endpoint([(bytes(damaged), len(damaged), 200)]) as (url, _requests, blobs):
            with self.assertRaises(zipfile.BadZipFile):
                self.fetch(url)
        self.assertEqual(len(blobs), 1)
        self.assertFalse(self.destination.exists())
        self.assert_scratch_removed()

    def test_https_redirect_cannot_downgrade_transport(self):
        request = download.urllib.request.Request("https://api.github.com/artifact", headers={
            "Authorization": f"Bearer {TOKEN}",
        })
        with self.assertRaisesRegex(ValueError, "insecure redirect"):
            download.ArtifactRedirect().redirect_request(
                request, None, 302, "Found", {}, "http://storage.example/artifact")

    def test_selection_is_pinned_to_recorded_run_and_source_name(self):
        calls = []
        def request(method, path):
            calls.append((method, path))
            return {"total_count": 1, "artifacts": [self.artifact]}
        selected = download.select_artifact(SHA, str(RUN), request)
        self.assertEqual(selected["id"], self.artifact["id"])
        self.assertEqual(calls, [("GET", f"{download.API_ROOT}/runs/{RUN}/artifacts?per_page=100")])
        for mutation in ({"expired": True}, {"workflow_run": {"id": RUN + 1}},
                         {"name": "cloud-test-binaries-" + "b" * 40}, {"size_in_bytes": 0},
                         {"digest": "sha256:invalid"}):
            original = copy.deepcopy(self.artifact)
            self.artifact.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                download.select_artifact(SHA, RUN, request)
            self.artifact = original

    def test_duplicate_source_artifacts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            download.select_artifact(SHA, RUN, lambda *_args: {
                "total_count": 2, "artifacts": [self.artifact, self.artifact],
            })


if __name__ == "__main__":
    unittest.main()
