# Desktop against an exact Cloud commit

Desktop's source trigger can send an optional `cloud_source_sha` alongside its
immutable Desktop `pr_ref`. This mode is for direct shared-crate pins to the
private server repository, including an unmerged server PR. Existing generated
Client mirror consumers omit the value and retain registry-image behavior.

The receiver requests the four images once with the existing private
`build_ci_images_self_hosted.yml` workflow. It forwards the successful build's
`cloud_image_run_id` and source SHA to Rust and Flutter; iOS remains unchanged.
The Desktop PR number is not passed as a Cloud PR number. This request omits
`ci_tools_sha`, so it does not compile the unrelated Cloud integration binaries.

Each runtime job checks out the requested Cloud commit and verifies:

- Desktop `pr_ref`, the actual Desktop checkout, and all four shared Rust pins.
- The actual CI tools checkout against that workflow's `github.sha`.
- The private build's repository, default-branch workflow, successful status,
  source-correlated title, and complete unexpired artifact set.
- Exact artifact IDs, Docker tags, Linux AMD64 architecture and OCI source
  revision labels before loading; then loaded image IDs and actual container IDs.

Only dependency service images are pulled. Startup uses `--no-build --pull never`
and a job-owned Compose override protects the four loaded images. Any verification
or load failure ends the job, with no registry fallback. Both optional inputs must
be absent for the legacy path; a partial or malformed pair is an error.

Timeline still builds its `self-host-af,commercial-ci,ci-test` Cloud flavor from
the verified Cloud checkout. The other three services use that same commit's
artifacts. Its source-built Cloud image receives the revision label and the final
container ID is recorded; the existing `self_hosted=true` server-info check remains.

The `desktop-cloud-manifest-*` job artifacts record Desktop, Cloud and CI commits,
the private image run and attempt, artifact IDs and runtime image IDs. A manifest
with `stage: runtime_verified` proves startup completed; it does not itself prove
the tests passed. Image artifacts expire after one day; an expired set requires a
new build of the same source, never a fallback to a registry tag.

## Activation and premerge validation

Automatic `repository_dispatch` runs use AppFlowy-CI's default branch. This CI PR
must be reviewed and merged before the new receiver path can serve ordinary
Desktop pushes. Publishing a Desktop direct-server pin does not activate an
unmerged receiver. No repository setting or merge is implicit in this change.

Before a CI merge, the existing child workflow IDs support manual validation on
the CI branch. First obtain a successful private image run for the exact server
commit (the server PR's integration image run can be reused). If a new image build
is needed, explicitly dispatch the existing private builder from `main`, supplying
`source_sha` and a unique numeric `<request>-<attempt>` request ID, and wait for its
success. Do not supply a Desktop PR as `pr_number`.

After publication is authorized, these commands validate the CI branch without
merging it or adding a task-branch pin to Desktop's shipping workflow:

```bash
gh workflow run rust_ci.yaml --repo AppFlowy-IO/AppFlowy-CI \
  --ref codex/desktop-exact-cloud-artifacts \
  -f pr_ref="$DESKTOP_SHA" -f pr_number="$DESKTOP_PR" \
  -f cloud_source_sha="$CLOUD_SHA" -f cloud_image_run_id="$IMAGE_RUN_ID"
gh workflow run flutter_ci.yaml --repo AppFlowy-IO/AppFlowy-CI \
  --ref codex/desktop-exact-cloud-artifacts \
  -f pr_ref="$DESKTOP_SHA" -f pr_number="$DESKTOP_PR" \
  -f cloud_source_sha="$CLOUD_SHA" -f cloud_image_run_id="$IMAGE_RUN_ID"
gh workflow run ios_ci.yaml --repo AppFlowy-IO/AppFlowy-CI \
  --ref codex/desktop-exact-cloud-artifacts \
  -f pr_ref="$DESKTOP_SHA" -f pr_number="$DESKTOP_PR"
```

Record the intended CI branch commit before dispatch, discover the actual runs,
and check their `headSha` plus each manifest's `ci_source_sha` against it. Check
`desktop_source_sha` and `cloud_source_sha` as well; a public workflow head SHA
alone does not identify either private source. Do not use a default-branch
registry-mode run as evidence for a new atomic API. Manual branch validation and
automatic receiver activation are separate decisions.

```bash
gh run list --repo AppFlowy-IO/AppFlowy-CI --workflow rust_ci.yaml \
  --branch codex/desktop-exact-cloud-artifacts --event workflow_dispatch \
  --json databaseId,headSha,status,conclusion,url,createdAt
gh run view "$RUN_ID" --repo AppFlowy-IO/AppFlowy-CI \
  --json headSha,event,status,conclusion,jobs,url
gh run download "$RUN_ID" --repo AppFlowy-IO/AppFlowy-CI \
  --pattern 'desktop-cloud-manifest-*' --dir "$EVIDENCE_DIR"
```

Use the equivalent run discovery for Flutter and iOS. Before each authorized push,
inspect the related source trigger, public receiver/children, and private image
builds and wait for all active runs for that source work to finish. Do not cancel
unrelated work to satisfy this gate. Periodic CI observations begin only after the
final Desktop push.

## Local checks

```bash
python3 -B -m unittest discover -s .github/scripts
python3 -B -m unittest discover -s scripts -p 'test_check_test_module_coverage.py'
```

The new exact-runtime tests use real Docker archive metadata and temporary Git
checkouts with controlled GitHub/command boundaries; they never start Docker or
dispatch Actions. The pre-existing Cloud binary archive tests require a Linux
AMD64 compiler/runtime and run in `workflow_checks.yaml` on Ubuntu. Local macOS
execution cannot validate that existing ELF-specific fixture.
