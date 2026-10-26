# Desktop CI uses prebuilt Cloud images

Desktop CI uses the published Docker Hub images configured by
`docker-compose-ci.yml` and `CLOUD_VERSION` (currently `latest-amd64`). Shared
Rust pins select the libraries compiled into Desktop; they do not request a
server image build. The Cloud checkout supplies Compose configuration only.

The receiver dispatches Flutter, Rust and iOS immediately without calling
`request_image_build.py`. Older Desktop triggers that send only
`cloud_source_sha` are normalized to registry mode with a notice. New Desktop
triggers omit both Cloud fields unless an explicit prebuilt image selection is
committed. Server-source CI retains its separate image-building workflows.

Registry startup first pulls images, then starts Compose with
`--no-build --pull never`. Missing images fail the job. Docker never compiles a
server as a fallback.

## Timeline image

Timeline uses the public self-hosted image
`appflowyinc/appflowy_cloud:latest-amd64` by default. Only its Cloud service is
overridden; other suites use the normal premium images. The existing
`/api/server-info` check still requires `self_hosted=true` before tests run.

Cloud Compose includes the published Gateway image, refreshed with the other
dependency images even when the four application images come from verified
archives. Hosted database creation and status APIs pass through Gateway. When
Cloud supplies `nginx/api-limits-self-host-ci.conf`, Timeline selects that fragment
to keep its self-hosted HTTP policy; older Cloud revisions remain supported.
The commercial Cloud integration lane also builds Worker, Search, and MCP with
their own `self-host-af` feature, so background writers select the same policy.

The Flutter workflow's `timeline_cloud_image` input or the optional
`DESKTOP_TIMELINE_CLOUD_IMAGE` repository variable can select another prebuilt
self-hosted image. No repository setting is needed for the default public image.
The image is pulled, never rebuilt. Missing or incompatible images fail the job
without falling back to source compilation or skipping Timeline tests.

## Optional exact-source images

An explicit pair of `cloud_source_sha` and `cloud_image_run_id` selects an
already-completed private integration-image build. The receiver forwards the
pair unchanged; it neither starts nor waits for a new build. An image run ID
without a source SHA, malformed values, or a non-immutable Desktop ref fails.
The child workflows also reject a partial pair instead of falling back.

Each exact runtime verifies:

- The immutable Desktop and Cloud checkouts, all four shared Rust pins and their
  resolved Git sources in Desktop's lockfile.
- For generated Client pins, committed source equality with the requested
  Server commit's sync output, including file modes. Only the standalone
  Client root lockfile is excluded; Desktop's lockfile is checked separately.
- The CI tools checkout against the workflow SHA.
- The private image workflow, successful status, source-correlated title and
  complete unexpired artifact set.
- Artifact IDs, Docker tags, Linux AMD64 architecture and OCI source revision
  labels, then loaded image IDs and the images attached to actual containers.

Timeline exact mode pulls its separately published flavor, requires its OCI
revision label to match `cloud_source_sha`, and pins the inspected local image
ID in the Compose override. It does not rebuild that flavor. Other application
services retain the verified image archives. Any verification failure ends the
job without fallback.

The `desktop-cloud-manifest-*` artifacts record verified source and image
identities. `stage: runtime_verified` proves startup, not test success. Expired
image artifacts require selection of another already-completed build.

## Validation and activation

Automatic repository dispatch uses AppFlowy-CI's default branch. Publishing a
feature branch does not activate the receiver change. Workflow-dispatch can
validate a CI branch before merge with an immutable Desktop `pr_ref`; omit the
Cloud pair for the default registry mode and supply `timeline_cloud_image` for
Flutter.

Local checks:

```bash
python3 -B -m unittest discover -s .github/scripts
python3 -B -m unittest discover -s scripts -p 'test_check_test_module_coverage.py'
```

The receiver tests execute the workflow's Bash parser. Startup tests execute the
real Python CLI with an isolated Docker boundary and check pull failures,
Timeline selection, and `--no-build` startup. Exact-image tests use real archive
metadata and temporary Git checkouts. Existing Cloud binary archive fixtures
require Linux AMD64 and run in `workflow_checks.yaml` on Ubuntu; they cannot be
validated using a macOS compiler/runtime.
