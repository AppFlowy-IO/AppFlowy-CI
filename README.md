# AppFlowy-CI

Cloud integration images and shared Cloud test binaries build on the ARM64 laptop through
AppFlowy-Cloud-Premium's `build_ci_images_self_hosted.yml`. Tests and coverage run on GitHub runners.
The requested Cloud ref resolves to one SHA for both images and tests. Four AMD64 image artifacts
are downloaded from the private Cloud Premium run with their existing `latest-amd64` tags.
For pull-request runs, the private run title includes the source PR number as
`CI images <run>-<attempt> (<sha>) [PR #<number>]`, while the parent AppFlowy-CI title keeps the
same number for correlation. Older image requests without a PR input retain the original title.

Merge the Cloud Premium builder's `ci_tools_sha` support before enabling this caller. The existing
`ADMIN_GITHUB_TOKEN` secret needs Cloud Premium access with Contents read and Actions write
(dispatch/cancel builds and download artifacts). The five runners are registered in
`AppFlowy-IO/AppFlowy-Cloud-Premium`; AppFlowy-CI needs no registered runner.

Manual runs default `image_builder` to `self-hosted`. Choose `github-hosted` to use the existing
image and shared test build jobs when the laptop is unavailable. For automatic runs, set repository
variable `CLOUD_IMAGE_BUILD_RUNNER=github-hosted` to use that fallback;
unset it or set `self-hosted` for laptop builds. `Wait for self-hosted images and Cloud test binaries`
dispatches one private build and waits up to 330 minutes, reporting progress about once per minute.
It links that build in its job summary and attempts to cancel only that build if interrupted. The four
`GitHub fallback` image jobs and GitHub's shared test compilation are skipped in this mode.

Release and integration builds have separate workflow queues and image caches. The laptop's
current build settings are documented in Cloud Premium's `doc/context/ci/self_hosted_runner_context.md`.
Cloud's CI image enables test features, and CI images use the `latest-amd64` artifact tags.
Versioned Docker Hub release images are a separate build with different settings, even for the same
source commit. Artifact retention is one day.

Automatic cleanup covers only `cloud_integration_ci.yaml` and its private laptop builds.
The integration run title records the Cloud PR and parent run attempt. Closing or merging that PR,
including a post-merge push build whose commit is associated with the PR, cancels the integration run
and its image/test build. Direct branch pushes without an associated PR remain active. Cancelling the
integration run also cancels its private build. Other workflows, including AppFlowy-Premium client CI,
are outside this cleanup.

Integration result notifications use `!cancelled()`: successful and failed runs report results,
while cancelled runs release their PR concurrency slot without waiting for a notification runner.

`cancel_obsolete_ci.yaml` checks every five minutes and after parent cancellations. For prompt PR-close
cleanup, Cloud Premium sends `ci-pr-closed` with `source_repository=AppFlowy-IO/AppFlowy-Cloud-Premium`
using `PUBLIC_REPO_TOKEN`. Cleanup uses `ADMIN_GITHUB_TOKEN` with Cloud Pull requests read and Actions
write on AppFlowy-CI and Cloud Premium, and executes only default-branch code. Post-merge branch
builds and release publishing continue. Older integration dispatches without identity need manual
cleanup; cleanup jobs can queue behind GitHub runner load.
Preview decisions locally with `python3 .github/scripts/ci_run_lifecycle.py`; add `--apply` to cancel.

Cloud integration tests use `https://localhost`. The workflow generates a short-lived
localhost certificate, installs its CA in the runner's system trust store, and
configures the server and Flutter client to use HTTPS/WSS. Public Form routes
reject plaintext HTTP, so changing the cloud URL back to HTTP breaks submission
tests. Keep certificate verification enabled when reproducing this setup.

The private run's fifth job cross-compiles the root Cloud tests once with `CLOUD_TEST_FEATURES`,
alongside its four image jobs. It uses the caller's pinned CI tools commit and `RUST_TOOLCHAIN`.
ARM64 Rust/C/C++ compilers produce `x86_64-unknown-linux-gnu` binaries; only the Ubuntu 24.04 startup
check uses QEMU to list tests without executing their bodies. The 12 root test lanes download the
private run's binary archive and execute their existing Rust test harnesses and filters without
invoking a compiler. The archive includes helper executables, generated runtime files and shared
libraries, and expires after one day.
Compilation uses `/home/runner/work/AppFlowy-CI/AppFlowy-CI` inside Docker to match GitHub's checkout
path. Consumers verify the source SHA, checkout path and Ubuntu 24.04 AMD64 runtime before use.
A missing or mismatched archive fails the job.
The final account-deletion script keeps its source-owned guard and uses the same precompiled binary.
Worker, Search and workspace-member lanes compile their different package selections as before.

The integration matrix has 15 jobs, with `max-parallel: 15`. Actions displays each job's topic:

| Topic | Suites |
| --- | --- |
| Core APIs and utilities | Root unit tests, files/Yrs, Redis/server-info, cache, folders, MCP, mentions and notifications |
| Workspace management | Workspace lifecycle, membership, invitations and workspace APIs |
| Permissions and spaces | Private-space migration, space ACLs, permission enforcement and sharing |
| Permissions and spaces 2 | Structured-space lifecycle, custom/PRD spaces, group permissions and supporting access suites |
| Import and publishing | Document/Notion imports, page views and publishing |
| Databases | Database tests, excluding the index test |
| Realtime collaboration | Collab integration tests |
| AI and authentication | AI, GoTrue, OIDC and user APIs |
| SQL persistence | SQL core, permissions, collab and workspace persistence |
| Shared libraries | Workspace-member and extracted Cloud package tests |
| Search | Database index and restore-search tests, root search integration tests, and the `appflowy-search` package |
| Signup whitelist | Isolated GoTrue whitelist and system-configuration tests |
| Auth | SCIM tests with Authentik and LDAP tests with OpenLDAP |
| Standalone HTTP targets | Every non-`main` Cloud Cargo integration target, including hosted-plan and migration fixtures |
| Worker service | Worker package tests |

Each topic has its own Docker stack. Modules and tests run serially within it; root integration
topics finish with seeded account deletion. Database history and restore-search run on the Search
stack before the other search modules, while the Databases topic skips those filters to avoid an
indexing backlog. Auth enables both Authentik and OpenLDAP profiles and runs SCIM and LDAP serially.
The standalone topic discovers every non-`main` Cargo test target, and the coverage gate requires a
new target to be selected explicitly or by `test_targets: "*"`. The 15-runner ceiling still leaves
room for workflow setup, coverage and cache-contract jobs.

The laptop keeps registry downloads, Git dependencies and the complete Cargo `target/` directory in
the dedicated `appflowy-premium-ci-integration-tests` BuildKit cache. It uses the machine's available
CPU and memory without Docker resource quotas. Its cache survives runner jobs and laptop restarts.

The GitHub fallback compiler and the three package lanes use Cargo dependency snapshots and
`sccache` with GitHub's cache v2 API. Each owns a snapshot and is its only writer.
Snapshots exclude installed Cargo tools and crates outside the workspace dependency graph. Keep
the pinned Rust toolchain and `CARGO_INCREMENTAL=0`; compiler caching requires incremental builds off.
The workflow pins the sccache action and binary, retries startup once, and falls back to ordinary
compilation if compiler-cache setup fails. Each GitHub compiling job's summary shows the Cargo cache hit,
writer role, and compiler cache hits/misses. GitHub cache storage is shared with other workflows;
check repository Actions cache usage if snapshots are repeatedly evicted. A new cache group or
dependency/toolchain change needs a successful writer run before later runs can reuse its snapshot.

Run the workflow regression checks with Python 3, PyYAML, and OpenSSL installed:

```sh
python3 -B -m unittest discover -s .github/scripts
```

The Cloud test-matrix coverage gate recognizes standard Rust, Tokio, and SQLx
test attributes. New test-bearing modules and standalone `tests/*.rs` targets must be assigned to
an integration or commercial matrix entry, including modules containing only `#[sqlx::test]` cases.
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
