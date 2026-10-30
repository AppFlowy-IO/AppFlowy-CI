# Presentation CI coverage

The implementation is in `cloud_integration_ci.yaml`. It adds four **Presentation contracts**
jobs and the required **Presentation acceptance (native stack and Gateway)** job. All use the
same immutable Cloud SHA as the image build and pinned CI tools from the public run. They wait
for the image build gate. They do not change the existing Shared libraries feature graph or
root binary archive, and they report failures through the existing integration aggregate.

On 2026-10-30, fetched AppFlowy-CI `origin/main` was
`5702dcbb32252bbf466b6d9a660d2aa2be165519`. It already runs the `appflowy-ai` service package in
**AI and authentication**, with source-image, Ollama/provider and commercial exclusions. It
still omits the three libraries below and does not select ignored database cases. This change is
based on that main commit and preserves its AI/Ollama wiring, encoded-cache phase selection, and
nightly-only Backup qualification.

## Package execution

Each package has its own runner, Cargo cache and Cargo invocation with its default features and
the `ci` profile. Cargo JSON identifies each executable and owning manifest; discovery and
execution reuse those binaries from their package directory. `appflowy-presentation` includes
all three Cargo integration targets as well as its library. The AI job selects only the
`appflowy_ai` binary's unit tests; live service integration targets stay in main's existing AI lane.
The three libraries also run Cargo doctests.

| Package | Ordinary test minimum | Explicit ignored database cases | Other execution |
| --- | ---: | ---: | --- |
| `appflowy-cloud-ai` | 100 | 70 | Two real PDF/video decoder fixtures |
| `appflowy-presentation` | 70 | 33 | Contract, prompt and research-export integration targets |
| `appflowy-ai` | 600 | 8 | Service binary unit tests, including presentation/research handler contracts |
| `gotrue` | 30 | 0 | Parameter validation and loopback transport/log redaction |

The exact ignored names are in
[`presentation_test_inventory.json`](../.github/scripts/presentation_test_inventory.json).
The 111 database cases cover:

- Cloud presentation sessions, viewer/write authorization, Admin role/session rechecks,
  source stamps/mapping, prompt activation, assessment/generation reuse, structural/imported maps,
  output validation, targeted regeneration, saved versions, export/download authorization,
  private-object lifetimes, retries, cancellation, usage reservation and accounting fences.
- Presentation persistence receipts, namespace/idempotency, leases/checkpoints, publication
  rollback, prompt/resource CAS, admission limits, cleanup/hard deletion, preparation caches,
  retained style/narrative dependencies, alias-index validity and bounded lock acquisition.
- Batched catalog/asset authorization, immutable context read bounds, saved-mention reuse misses,
  cross-member import identity and independent housekeeping progress during blocked workspace reads.
- AI configuration ordering/seed reconciliation, credential tombstones, embedding profile
  registry identity, cross-pool configuration serialization and atomic persistent rate limiting.

These are real PostgreSQL tests. Many Cloud contracts deliberately replace source, model, object
or Gateway boundaries with deterministic test dependencies; their names mentioning hosted usage
do not prove a running Billing or Gateway deployment.

Each job owns an empty `pgvector/pgvector:pg16` service at `127.0.0.1:5432/presentation_ci`.
`DATABASE_URL`, `APPFLOWY_PRESENTATION_TEST_DATABASE_URL` and `TEST_DATABASE_URL` explicitly target
it, with a create-database role. SQLx and schema fixtures create their own test namespaces. No
application Compose stack or seeded/developer database is reused. `redis-server` is installed
for source-owned loopback fixtures. `ffmpeg`, `ffprobe` and `pdftoppm` are installed, version-checked
and supplied through explicit test executable variables before the two media bodies execute.

`require_tests` rejects missing required names or insufficient ordinary discovery. New ignored
tests fail until their prerequisite owner is reviewed. Every executed target must report exactly
the discovered number of passed tests and zero ignored cases; each supported ignored case runs
serially with `--exact --ignored` and must report exactly one pass. Ordinary selections explicitly
exclude ignored cases and verify the resulting inventory before execution.

## Owning acceptance lane

`tests/presentation_acceptance.rs` is excluded from the generic standalone loop and default root
archive execution. Explicit archive execution without an ignored selection is rejected. The coverage
checker recognizes Actix test attributes and requires `OWNED_TEST_TARGETS: presentation_acceptance`
in the dedicated lane; the standalone wildcard is insufficient.

The lane defaults to the published `AppFlowy-IO/AppFlowy-Cloud-Service` commit
`44bcd0f16baa75d95d8d4c2ea873cd246498738a`. Its Billing/Gateway migrations and bridge test were
checked against the local acceptance fixture on 2026-10-30. Optional repository variables override
this pair when a different reviewed Service revision is required:

- `PRESENTATION_SERVICE_REPOSITORY`: the private repository containing
  `appflowy-cloud-billing/` and `appflowy-cloud-gateway/`.
- `PRESENTATION_SERVICE_SHA`: a reviewed immutable 40-character commit with the matching
  Billing/Gateway migrations and the Gateway bridge test. `ADMIN_GITHUB_TOKEN` must read it.

An empty or invalid resolved pin, a mismatched checkout, missing migrations/bridge, an ignored-name mismatch,
failed readiness, or a zero-test execution fails this job. The job does not infer a developer's
Service checkout or silently skip acceptance. Sources older than the entire presentation suite
are explicitly inapplicable; partial source suites fail capability detection.

The runner installs SQLx CLI 0.8.6, Docker-accessible fixture dependencies and media tools. It runs:

1. `script/test_presentation_acceptance.py --profile ci --service-root <pinned checkout>`.
   The source-owned runner starts private PostgreSQL 16, GoTrue, Redis, MinIO and the actual hosted
   Cloud executable on random ports, applies history-aware Cloud and actual Service-owner
   migrations, and cleans up only owned resources. Three exact test bodies are required:
   `native_folder_view_sources_reject_private_and_trashed_ancestry_without_collab_rows`,
   `production_sources_reauthorize_and_private_objects_use_real_s3`, and
   `web_research::web_search_toggle_preparation_cache_and_failure_contract`. These cover actual
   native ACL/trash/ancestry, source/cache revisions, database views, private S3, and the local
   deterministic research-provider HTTP contract.
2. `selected_pages_and_captured_mentions_share_bounded_current_folder_checks` runs separately with
   `--exact --ignored` on this acceptance lane's explicit PostgreSQL 16 service. Its SQLx fixture
   creates an empty database and applies full Cloud history, then verifies bounded current Folder
   checks over 100 pages and immediate trash/mention revocation. It must report exactly one pass.
3. `script/test_presentation_gateway_contract.py` with separately built default-feature
   Cloud-AI and Gateway test binaries. It requires both ignored bridge names, owns its Redis and
   SQLx database, and checks the actual Gateway router → hosted usage adapter → Cloud HTTP
   error contract. The source runner must complete and its Cloud test must report one pass.

The default Service commit is published and its migration/bridge files match the local fixture;
private repository checkout permissions and the complete lane remain unverified in remote CI.
Checkouts deliberately do not persist credentials. The native acceptance command separately
supplies the same read token to Cargo's Git subprocesses through process-local Git configuration,
scoped to HTTPS repositories under `github.com/AppFlowy-IO/`. No token is written to Git config
or a credential file; its Basic-auth encoding is masked before child execution. A missing token
fails before compilation. This fixes the private `app-error` fetch failure in run `41077087642`;
successful checkout alone did not authenticate Cargo's transitive Git fetches.
A failed prerequisite fails the job instead of skipping it. Even a successful bridge plus migrated
full-stack fixture would not establish live Billing/Stripe webhook or production entitlement
coverage. Real provider quality, the paid official-image test, Office/Web pixel parity, deployment
builds, and the complete field/row/media matrix remain outside these additions.

## Local checks

On 2026-10-30, the source-owned native acceptance runner passed all three exact cases using freshly
built hosted Cloud binaries and an owned PostgreSQL 16/GoTrue/Redis/MinIO stack with actual Cloud and
Service migrations. The 70 selected Cloud PostgreSQL cases passed, including two corrected failures;
15 accounting cases passed again after the final stored-reservation compatibility change. The
published Service pin does not by itself prove its Gateway bridge: that bridge and the complete
remote lane remain unrun in this review. The 19 focused runner tests, 10 coverage-checker tests and
actionlint passed locally.

```sh
python3 -B -m unittest discover -s .github/scripts -p 'test_presentation_ci.py'
python3 -B -m unittest discover -s scripts -p 'test_check_test_module_coverage.py'
python3 -B -m unittest discover -s .github/scripts
```

The focused suite uses a small real Cargo workspace to prove default features, integration target
discovery, package working directories and ignored body execution. It needs Rust and a C compiler.
The existing full binary-archive fixture assumes a Linux runtime and currently fails on macOS's
canonical temporary path before the archive tests start; report that separately from the new
checks. No Cloud Rust suite, Service fixture, remote CI run or deployment is certified by the
local Python/structural checks.
