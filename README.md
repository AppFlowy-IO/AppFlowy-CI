# AppFlowy-CI

Cloud integration images build on the ARM64 laptop through Premium's
`build_ci_images_self_hosted.yml`; all test, coverage and cache jobs stay on `ubuntu-latest`.
The requested Cloud ref resolves to one SHA for both images and tests. Four AMD64 image artifacts
are downloaded from the private Premium run with their existing `latest-amd64` tags.

The Premium builder workflow must be available before using laptop builds. The existing
`ADMIN_GITHUB_TOKEN` secret needs Premium repository access with Contents read and Actions write
(dispatch/cancel builds and download artifacts). The five runner registrations remain in Premium;
AppFlowy-CI needs no registered runner.

Manual runs default `image_builder` to `self-hosted`. Choose `github-hosted` to use the existing
image build jobs when the laptop is unavailable or an older Cloud ref is unsupported. For automatic
runs, set repository variable `CLOUD_IMAGE_BUILD_RUNNER=github-hosted` to use that fallback;
unset it or set `self-hosted` for laptop builds. `Wait for self-hosted images` dispatches one private
build and waits up to 330 minutes, reporting status changes and progress about once per minute.
It links that build in its job summary and attempts to cancel only that build if interrupted. The four
`GitHub fallback` image jobs are skipped in this mode.

Release and integration builds have separate workflow queues and image caches. The laptop's
current build settings are documented in Premium's `doc/context/ci/self_hosted_runner_context.md`.
Cloud's CI image enables test features, and CI images use the `latest-amd64` artifact tags.
Versioned Docker Hub release images are a separate build with different settings, even for the same
source commit. Artifact retention is one day.

CI run titles record the source repository, PR number and parent run attempt.
Closing or merging a PR requests cleanup through `cancel_obsolete_ci.yaml`; cancelling a webhook
or image-request run also cancels its children, including the laptop image build. A five-minute
schedule catches delayed dispatches and retries cleanup. Only identified PR/parent runs are cancelled;
post-merge branch builds and release publishing continue.

Both private source repositories send `ci-pr-closed` using their existing `PUBLIC_REPO_TOKEN`.
Cleanup uses `ADMIN_GITHUB_TOKEN` with Pull requests read on the source repositories and Actions write
on AppFlowy-CI and Cloud Premium. It executes only default-branch code. Merge the AppFlowy-CI changes
first, then the source notification workflows. Older dispatches without the new identity cannot be
matched safely and need manual cleanup. Cleanup jobs can themselves queue behind GitHub runner load.
Preview decisions locally with `python3 .github/scripts/ci_run_lifecycle.py`; add `--apply` to cancel.

Cloud integration tests use `https://localhost`. The workflow generates a short-lived
localhost certificate, installs its CA in the runner's system trust store, and
configures the server and Flutter client to use HTTPS/WSS. Public Form routes
reject plaintext HTTP, so changing the cloud URL back to HTTP breaks submission
tests. Keep certificate verification enabled when reproducing this setup.

Run the workflow regression checks with Python 3, PyYAML, and OpenSSL installed:

```sh
python3 -B -m unittest discover -s .github/scripts
```

The Cloud test-matrix coverage gate recognizes standard Rust, Tokio, and SQLx
test attributes. New test-bearing modules must be assigned to an integration or
commercial matrix entry, including modules containing only `#[sqlx::test]` cases.
Run the coverage-checker regressions with Python 3:

```sh
python3 -B -m unittest discover -s scripts -p 'test_check_test_module_coverage.py'
```

Each Cloud root integration lane ends with `script/test_final_account_deletion.sh` from
the checked-out Cloud source. It explicitly runs an ignored API test that deletes the shared
`eva@appflowy.io` snapshot account and verifies private-space recovery. Each matrix job has its
own restored database; keep this step after all ordinary tests. Package-only lanes and the
commercial reset suite do not use this final fixture check. Older Cloud refs without the runner
remain supported.

The Cloud integration workflow also runs the encoded-collab cache contracts and cache dashboard
checks from the requested private Cloud ref. These jobs use disposable PostgreSQL, private Redis
and S3 fixtures, and four Rust test threads; they do not wait for the application images. The Rust
runner first builds the ordinary indexing library, then runs the selected cache and Worker tests.
The dashboard job validates Prometheus queries and both CI Compose cache configurations.

Older Cloud refs with none of `script/test_encoded_cache.sh`,
`script/test_encoded_cache_dashboard.py`, and `script/test_encoded_cache_compose.py` skip these
jobs. A partial suite fails setup. Cache, image-build, coverage, and integration failures all feed
the final integration notification. Regression checks for this wiring are included in the
`.github/scripts` unittest command above.

When migrating an existing Cloud branch to these jobs, land the CI workflow change before
removing that branch's `.github/workflows/encoded-cache.yml` scheduler.
