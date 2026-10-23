#!/usr/bin/env python3
"""Download the exact private build's test archive before the existing restore checks run."""

import gzip
import hashlib
import http.client
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from request_image_build import API_ROOT, REPOSITORY, api


ARCHIVE_NAME = "cloud-tests.tar.gz"
ATTEMPTS = 3
ATTEMPT_SECONDS = 600
CHUNK_BYTES = 1024 * 1024


class TransferFailure(RuntimeError):
    """An incomplete or damaged transfer may succeed with a fresh signed download URL."""


class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        redirected = super().redirect_request(request, response, code, message, headers, url)
        if redirected is not None:
            if request.type == "https" and urllib.parse.urlsplit(url).scheme != "https":
                raise ValueError("Artifact download refused an insecure redirect")
            # GitHub redirects to signed blob storage; never send its API token to that host.
            redirected.remove_header("Authorization")
        return redirected


def select_artifact(source_sha, run_id, request=api):
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("Expected an immutable 40-character Cloud commit")
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        raise ValueError("Expected the private build's positive run ID")
    name = f"cloud-test-binaries-{source_sha}"
    result = request("GET", f"{API_ROOT}/runs/{run_id}/artifacts?per_page=100")
    if result["total_count"] > 100:
        raise ValueError("Private build has too many artifacts to select unambiguously")
    matches = [artifact for artifact in result["artifacts"] if artifact["name"] == name]
    if len(matches) != 1:
        raise ValueError("Expected exactly one source-specific Cloud test artifact")
    artifact = matches[0]
    if artifact["expired"] or artifact["workflow_run"]["id"] != int(run_id):
        raise ValueError("Cloud test artifact is expired or belongs to a different private run")
    if not isinstance(artifact["id"], int) or artifact["id"] <= 0:
        raise ValueError("Cloud test artifact has an invalid ID")
    if not isinstance(artifact["size_in_bytes"], int) or artifact["size_in_bytes"] <= 0:
        raise ValueError("Cloud test artifact has no positive recorded size")
    digest = artifact.get("digest")
    if digest and not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Cloud test artifact has an unsupported digest")
    return artifact


def transfer(url, destination, artifact, token, *, timeout=ATTEMPT_SECONDS):
    """Stream to an unpublished file, bounding each read and the whole transfer."""
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AppFlowy-CI-test-artifact",
    })
    opener = urllib.request.build_opener(ArtifactRedirect())
    deadline = time.monotonic() + timeout
    expected_size = artifact["size_in_bytes"]
    digest = hashlib.sha256()
    received = 0
    try:
        with opener.open(request, timeout=min(timeout, 30)) as response, destination.open("wb") as output:
            while chunk := response.read1(CHUNK_BYTES):
                received += len(chunk)
                if received > expected_size:
                    raise TransferFailure("Downloaded bytes exceed the recorded artifact size")
                if time.monotonic() >= deadline:
                    raise TransferFailure("Artifact download exceeded its attempt deadline")
                digest.update(chunk)
                output.write(chunk)
    except urllib.error.HTTPError as error:
        # Throttling and server failures can succeed on a fresh request.
        if error.code in (408, 429) or error.code >= 500:
            raise TransferFailure(f"Artifact download HTTP {error.code}") from None
        raise ValueError(f"Artifact download HTTP {error.code}; verify artifact access") from None
    except (urllib.error.URLError, http.client.HTTPException, socket.timeout, ConnectionError):
        # Do not include signed URLs, request headers or access tokens in diagnostics.
        raise TransferFailure("Artifact download was interrupted") from None
    if received != expected_size:
        raise TransferFailure(f"Incomplete artifact: received {received} of {expected_size} bytes")
    actual = "sha256:" + digest.hexdigest()
    if artifact.get("digest") and actual != artifact["digest"]:
        raise TransferFailure("Artifact SHA256 differs from GitHub's recorded digest")
    print(f"Verified artifact {artifact['id']}: {received} bytes, {actual}", flush=True)


def unpack_verified(download, destination):
    """Check both archive layers before publishing the one expected payload atomically."""
    with zipfile.ZipFile(download) as bundle:
        if bundle.namelist() != [ARCHIVE_NAME]:
            raise ValueError("Cloud test artifact must contain only cloud-tests.tar.gz")
        info = bundle.getinfo(ARCHIVE_NAME)
        if info.is_dir() or info.flag_bits & 1:
            raise ValueError("Cloud test archive must be an ordinary unencrypted file")
        with bundle.open(info) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output, CHUNK_BYTES)
    # A valid ZIP digest cannot establish the producer's inner gzip is complete. Read through
    # its trailer too; a malformed published artifact is a hard error, not a download retry.
    with gzip.open(destination, "rb") as source:
        while source.read(CHUNK_BYTES):
            pass


def download_artifact(artifact, destination, token, *, attempts=ATTEMPTS,
                      timeout=ATTEMPT_SECONDS, wait=time.sleep, url=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = url or f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    with tempfile.TemporaryDirectory(prefix=".cloud-test-download-", dir=destination.parent) as scratch:
        downloaded = Path(scratch) / "artifact.zip"
        for attempt in range(1, attempts + 1):
            try:
                transfer(url, downloaded, artifact, token, timeout=timeout)
                break
            except TransferFailure as error:
                downloaded.unlink(missing_ok=True)
                print(f"Artifact download attempt {attempt}/{attempts} failed: {error}", flush=True)
                if attempt == attempts:
                    raise TransferFailure("Cloud test artifact download exhausted its attempts") from None
                wait(min(2 ** attempt, 10))
        archive = Path(scratch) / ARCHIVE_NAME
        unpack_verified(downloaded, archive)
        archive.replace(destination)
    print(f"Published verified Cloud test archive: {destination}", flush=True)


def main():
    artifact = select_artifact(os.environ["SOURCE_SHA"], os.environ["PRIVATE_BUILD_RUN_ID"])
    destination = Path(os.environ["RUNNER_TEMP"]) / "cloud-test-binaries" / ARCHIVE_NAME
    download_artifact(artifact, destination, os.environ["GH_TOKEN"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TransferFailure, OSError, EOFError, zipfile.BadZipFile) as error:
        # The selected exception messages do not contain API credentials or signed URLs.
        print(f"Cloud test artifact validation failed: {error}", file=sys.stderr)
        sys.exit(1)
