# AppFlowy-CI

Pull requests in this repository run **Workflow checks**, which validates the CI scripts and
test-selection configuration without building an application. New commits cancel superseded
checks for the same PR; other PRs, main pushes, and manual checks have separate concurrency groups.
The check runs for every PR so documentation and Docker-only changes have a consistent result.

Application suites run through their existing private-repository dispatch events. For an intentional
end-to-end check of a CI branch, dispatch the relevant workflow manually with that CI branch as
`--ref` and the private source revision as `pr_ref`, for example:

```sh
gh workflow run flutter_ci.yaml --repo AppFlowy-IO/AppFlowy-CI \
  --ref <ci-branch> -f pr_ref=<desktop-commit>
```

Choose only the affected suite (`flutter_ci.yaml`, `rust_ci.yaml`, or `ios_ci.yaml`) and supply any
additional runtime inputs it needs. Workflow-script tests do not replace an end-to-end validation
when changing build or runtime behavior. Ordinary private-source CI keeps its full test matrices.

Manual Rust/Flutter validation from a non-default CI branch has its own concurrency
group per CI branch and private PR, so the automatic main workflow cannot cancel it
while testing that same PR. A newer manual attempt in the same group still replaces
the old one. Cancellation does not queue new PDF reporting jobs; an ordinary failed
preparation still reports a failing PDF status.

PR #97 exposed why these triggers are separate: its CI/Gateway configuration changes started
41 Flutter, four Rust, and four iOS jobs because those workflows accepted every CI-repository PR
and defaulted their private source checkout to `main`.

Desktop can optionally test direct shared-crate server pins against an exact
private Cloud image build. See [Desktop exact Cloud runtime](docs/desktop-exact-cloud.md)
for input provenance, legacy behavior and CI-branch validation before receiver activation.

Desktop PDF exports have three focused commit statuses: `PDF export regression (Rust)`,
`PDF export regression (UI)` and `PDF export regression (Cloud UI)`. The source-owned
`frontend/rust-lib/integration-test/pdf_export_contract.json` names the required native
tests and BDD scenarios. The same required Rust gate runs all 12 `database_pdf_export`
event cases and seven `pdf_export_columns` recovery components without module filters;
passing events cannot hide a failing component suite. Desktop CI prepares only the
Linux application and FFI artifact. The local PDF UI BDD reuses that artifact on
Ubuntu with PDFium and an Xvfb display; the cold-cloud BDD remains in the existing
Linux `pdf_export` cloud suite. Windows and macOS preparation jobs are omitted.
Machine test events must show the contracted scenarios completed without skips,
and native process failures are preserved. Inventories, results, logs and saved
PDF/CSV files are retained for seven days.

The status bridge requires an immutable `pr_ref`, a matching checkout and current
private PR head, and a passing current gate step. Legacy refs with no PDF tests or
contract receive an explicit not-applicable status; removing the contract or tests
in a PR cannot use that fallback. Test execution does not receive the bridge's token.
To validate a CI branch's statuses, dispatch Rust and Flutter with both `pr_ref=<SHA>`
and `pr_number=<private-PR>`. Merge the workflow and Desktop contract changes and verify
the same Desktop SHA before making these statuses required in repository rules.
Require up-to-date branches with the main-only status rule. Once main adopts the
contract, an older head's legacy NA statuses must not allow untested changes to
merge with the newer main revision.

Flutter's Linux jobs configure APT from the same CI revision as the workflow.
Run `39783354102` lost four desktop jobs after the Azure Ubuntu mirror spent
42–59 minutes downloading prerequisites; recent successful test steps took
17–31 minutes. The disposable runners now use Ubuntu's primary archive for
those mirror entries, with one retry and 20-second HTTP/HTTPS inactivity limits.
Prerequisite installation has a separate 10-minute deadline. The test matrix,
45-minute test limits, package signatures, and repository suites stay intact.
The configuration script refuses to run outside a GitHub Linux runner; its
local tests use temporary files without root or network access.

Cloud integration images and shared Cloud test binaries build on the ARM64 laptop through
AppFlowy-Cloud-Premium's `build_ci_images_self_hosted.yml`. Ordinary integration tests and coverage
run on GitHub runners; Backup restore qualification runs nightly in Cloud Premium (see **Backup**).
The requested Cloud ref resolves to one SHA for both images and tests. Four AMD64 image artifacts
are downloaded from the private Cloud Premium run with their existing `latest-amd64` tags.
For pull-request runs, the private run title includes the source PR number as
`CI images <run>-<attempt> (<sha>) [PR #<number>]`, while the parent AppFlowy-CI title keeps the
same number for correlation. Older image requests without a PR input retain the original title.

Merge the Cloud Premium builder's `ci_tools_sha` support before enabling this caller. The existing
`ADMIN_GITHUB_TOKEN` secret needs Cloud Premium access with Contents read and Actions write
(dispatch/cancel builds and download artifacts). The five runners are registered in
`AppFlowy-IO/AppFlowy-Cloud-Premium`; AppFlowy-CI needs no registered runner.

Every integration run uses the private ARM64 self-hosted build. The workflow dispatches one private
build and waits up to 330 minutes, reporting progress about once per minute. It links that build in
its job summary and attempts to cancel only that build if interrupted. If no self-hosted runner is
available, the build remains queued or fails; it never silently moves compilation to a GitHub-hosted
runner. The integration test matrix and its package-only Rust lanes still run on GitHub-hosted
runners after the private images and shared Cloud test binaries are ready.

Release and integration builds have separate workflow queues and image caches. The laptop's
current build settings are documented in Cloud Premium's `doc/context/ci/self_hosted_runner_context.md`.
Cloud's CI image enables test features, and CI images use the `latest-amd64` artifact tags.
Versioned Docker Hub release images are a separate build with different settings, even for the same
source commit. Artifact retention is one day.

Automatic cleanup covers only `cloud_integration_ci.yaml` and its private laptop builds.
The integration run title records the Cloud PR and parent run attempt. Closing or merging that PR,
or cancelling its parent webhook, cancels the integration run and its image/test build. Cancelling the
integration run also cancels its private build. Other workflows, including AppFlowy-Premium client
CI, are outside this cleanup.

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
Consumers select the exact source-named artifact from the recorded private build, then verify its
recorded byte count, SHA256 digest when available, ZIP CRC and inner gzip trailer before making it
available to restore. Downloads get at most three attempts of ten minutes each. Interrupted or
slow transfers resume their unpublished prefix using a fresh signed URL and a validated HTTP byte
range; checksum mismatches restart from zero. Servers that ignore Range also restart from zero.
API authorization, malformed artifacts and source/manifest mismatches fail without retries.
The token is removed before following signed blob-storage redirects. This
addresses partial downloads that previously reported success and failed later with gzip EOF; test
failures are never retried by this handoff. A missing or mismatched archive fails the job.
The final account-deletion script keeps its source-owned guard and uses the same precompiled binary.
The SQL persistence topic has a 90-minute job budget for artifact transfer, setup and cleanup;
its test execution still has the same 50-minute deadline as the other topics.
Worker, Search and workspace-member lanes compile their different package selections as before.
The configuration job also runs the source-owned AI web-search fixture tests when that test file
exists; older Cloud revisions remain supported, while present fixture failures fail the job. The
AI lane reads the fixture's opaque source URL and passes it to the strict citation assertions, so
an answer containing a guessed public URL cannot stand in for a successful tool response.

Presentation contracts have separate package builds and an explicit ignored-test inventory, with
owned PostgreSQL and installed media tools. The root `presentation_acceptance` target belongs to
the required native/Gateway acceptance job rather than the generic standalone wildcard. That job
uses a published Service commit by default and rejects invalid overrides or missing prerequisites;
ordinary package tests do not establish hosted Gateway or Billing coverage.
See [Presentation CI coverage](docs/presentation-ci.md).

The Search, Worker, Shared libraries and Directory self-host contracts lanes start a separate Redis
on a random loopback port and export `APPFLOWY_TEST_REDIS_URL` only to tests. Application consumers use the Compose Redis,
so they cannot drain admission-test streams and hide unexpected enqueueing. An always-run step
removes this fixture, including failed jobs. Shared libraries also runs the MCP and generic Redis
worker package regressions.
The fixture starts with `maxmemory=512mb` and `maxmemory-policy=noeviction`: percentage-based
Search admission requires a known capacity, and collaborative stream entries must not be evicted.
Search mutation/recovery fixtures create their own empty databases through Cloud's history-aware
migration runner; an explicitly supplied migrated template is only a local optimization.

Shared libraries runs the full `appflowy-cloud-directory` and `appflowy-mcp-core` package tests,
including SCIM protocol, group-status SQL and workspace-bound MCP token regressions. When the
managed-user production modules are present in the selected Cloud revision, inventory guards
require at least 51 SCIM protocol tests plus the SCIM group-limit policy test (52 SCIM-related
cases total), 11 default-feature group-status and 3 MCP token tests, with none ignored.
The separate **Directory self-host contracts** runner compiles and runs the directory status
suite with `self-host-af`, requiring all 12 status tests including the exact admin-audit rollback
case in its nonignored inventory. Moving that feature graph to its own runner keeps its compilation
out of the Shared libraries budget without changing the hosted member-package selection, features
or unfiltered test/doc-test coverage. Each feature graph runs once and owns a separate Cargo cache;
these member binaries compile on their runner and do not use the root Cloud binary archive.
Older Cloud revisions still run their full package suites without guards for missing features.
The SQL fixtures use the job's PostgreSQL; MCP token fixtures use the isolated test Redis above.
These native package tests use the same resolved Cloud SHA as that run's images and root tests.

Automatic repository dispatch uses this repository's default-branch workflow. A CI pull request
must merge before its coverage changes apply to ordinary Cloud PR runs; it may merge before the
managed-user server changes because the new count guards are source-gated. Cloud dispatches a
branch name, which this workflow resolves once: verify the resolved source SHA matches the Cloud
PR head when reviewing results. The CI workflow/tools SHA is the public run's `github.sha`.

The integration matrix has 16 jobs, with `max-parallel: 15`. Actions displays each job's topic:

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
| Directory self-host contracts | The `self-host-af` Directory status SQL suite, including admin-audit rollback |
| Search | Database index and restore-search tests, root search integration tests, and the `appflowy-search` package |
| Signup whitelist | Isolated GoTrue whitelist and system-configuration tests |
| Auth | SCIM tests with Authentik and LDAP tests with OpenLDAP |
| Standalone HTTP targets | Generic non-`main` Cloud Cargo integration targets; presentation acceptance uses its dedicated fixture job |
| Worker service | Worker package tests |

Each topic has its own Docker stack. Modules and tests run serially within it; root integration
topics finish with seeded account deletion. Database history and restore-search run on the Search
stack before the other search modules, while the Databases topic skips those filters to avoid an
indexing backlog. Auth enables both Authentik and OpenLDAP profiles and runs SCIM and LDAP serially.
The standalone topic discovers generic non-`main` Cargo test targets, and the coverage gate requires a
new target to be selected explicitly or by `test_targets: "*"`. Presentation acceptance requires
`OWNED_TEST_TARGETS` in its dedicated job. The 15-runner ceiling still leaves
room for workflow setup, coverage and cache-contract jobs.

The AI package runs on the host against the AI container built from the same Cloud source.
Before that package runs, CI reads `APPFLOWY_GOTRUE_JWT_SECRET` from the running `ai` container
and passes it only to the package test process. Compose derives it from `GOTRUE_JWT_SECRET`;
the host's `.env` does not contain the service-side name. A failed read or blank secret stops
the lane before admin tests run. The secret is not printed, and older Cloud sources without
an AI image continue to skip the package without reading container configuration.

**Backup** is not part of this workflow. Cloud Premium's `backup-release-tests.yml` runs its
complete `suite=ci` restore qualification once a night on Cloud main, on the private isolated VM,
and can still be dispatched there for one commit. A qualification takes two to four hours on the
VM's single Backup slot. While every integration run requested one, requests (7–26 a day in
October 2026) outnumbered what the slot could clear (6–10), so the public waiter timed out after
5 h 45 min, failed the integration result, and left its private request queued. Cloud owns the
Backup tests, fixtures and private evidence; see its `doc/context/ci/README.md`. The obsolete-CI
cleanup still cancels only private image builds, never a Backup run.

The laptop keeps registry downloads, Git dependencies and the complete Cargo `target/` directory in
the dedicated `appflowy-premium-ci-integration-tests` BuildKit cache. It uses the machine's available
CPU and memory without Docker resource quotas. Its cache survives runner jobs and laptop restarts.

The separate **Cache and snapshot recovery** and **Permission contracts** jobs compile on
GitHub-hosted runners. Older Cloud refs use one **Cache, snapshot and permission contracts (all)**
job. Each phase retains its own Cargo dependency snapshot, preserving the source runner's separate
package feature graphs and existing test selections. The cache action can save completed dependency
builds after an ordinary test failure, so a failed contract does not automatically discard that
compilation work. It does not guarantee a save after cancellation, an abrupt timeout or runner loss,
and no wall-clock speedup has been measured for this change.

These phase caches keep the action's dependency-only cleanup: workspace crates and incremental
build outputs are excluded, and no extra directories or binary archives are added. Distinct phase
keys avoid parallel jobs competing to save one immutable cache entry. This permits reuse on later
runs of each phase; it does not transfer compiled artifacts between the two jobs in the same run.
Dependency snapshots still consume GitHub cache storage and runner disk during restore. Cleaning
the private build host does not clean these hosted-runner caches; review Actions cache usage
separately and retain useful dependency snapshots when reclaiming disk space.

The four package lanes use Cargo dependency snapshots and `sccache` with GitHub's cache v2 API.
Each owns a snapshot and is its only writer.
Snapshots exclude installed Cargo tools and crates outside the workspace dependency graph. The
workflow disables dev/test debug symbols and does not persist the root compiler's `target/`
snapshot; `sccache` retains reusable compiler outputs instead. Keep the pinned Rust toolchain and
`CARGO_INCREMENTAL=0`; compiler caching requires incremental builds off.
The workflow pins the sccache action and binary, retries startup once, and falls back to ordinary
compilation if compiler-cache setup fails. Each GitHub compiling job's summary shows the Cargo cache hit,
writer role, and compiler cache hits/misses. GitHub cache storage is shared with other workflows;
check repository Actions cache usage if snapshots are repeatedly evicted. A new cache group or
dependency/toolchain change needs a successful writer run before later runs can reuse its snapshot.

Run the workflow regression checks with Python 3, PyYAML, and OpenSSL installed:

```sh
python3 -B -m unittest discover -s .github/scripts
```

The Cloud test-matrix coverage gate recognizes standard Rust, Tokio, SQLx, and Actix
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
and S3 fixtures, and four Rust test threads. They start only after the selected image builder and
shared test-binary gate succeeds, and are skipped when image construction is cancelled or fails, so
they cannot keep a failed image request running. The Rust runner first builds the ordinary indexing
library, then runs the selected cache and Worker tests.
The dashboard job validates Prometheus queries and both CI Compose cache configurations.

Cloud refs with `script/cache_test_phases.json` must advertise schema version 1 and exactly the
`cache` and `permission` phases. CI runs them on separate runners with separate Cargo caches and
the existing 120-minute limit per phase. The source runner owns each phase's selections and package
feature graphs; this avoids the combined lane's observed two-hour compilation timeout without
unifying Cargo features. Both matrix results feed the required `cache-contracts` result, and one
failure does not cancel its sibling. Full-run completion within the bound still needs CI evidence.
Older refs without the manifest run their combined `all` gate once. An unsupported or incomplete
manifest fails setup instead of skipping contracts or duplicating the full gate.

Older Cloud refs with none of `script/test_encoded_cache.sh`,
`script/test_encoded_cache_dashboard.py`, and `script/test_encoded_cache_compose.py` skip these
jobs. A partial suite fails setup. Cache, image-build, coverage, and integration failures all feed
the final integration notification. Regression checks for this wiring are included in the
`.github/scripts` unittest command above.

When migrating an existing Cloud branch to these jobs, land the CI workflow change before
removing that branch's `.github/workflows/encoded-cache.yml` scheduler.

Desktop's Flutter and Rust hosted member fixtures receive `APPFLOWY_TEST_DATABASE_URL`
for the job's disposable Compose PostgreSQL and use `psql` to provision a paid plan
only for their newly created workspace. This keeps multi-member permission tests
compatible with hosted Free-plan limits. Local runs must explicitly provide their
disposable test database; the fixtures do not infer developer database settings.
