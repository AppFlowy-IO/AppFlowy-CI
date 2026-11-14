# Automatic iOS row-page Cloud coverage

`ios_ci.yaml` keeps its four existing matrix jobs. Runners 1–3 retain their ARM
macOS hosts and local suites. Runner 4 uses `macos-15-intel`, the Desktop
`development-ios-x86_64-sim` Rust profile, and an Intel iOS simulator. It runs the
existing `mobile_runner_4.dart` first, then provisions a job-owned Cloud stack on
that same host and runs `integration_test/mobile_row_page_cloud_runner.dart`.
The focused runner initializes the integration-test binding and runs the two
mobile row-page scenarios; the aggregate mobile suite is not repeated.

Runner 4 verifies the built FFI archive is x86_64 and writes a simulator-only
architecture override under `RUNNER_TEMP`. Its prebuild, local tests, and Cloud
tests pass that file through `XCODE_XCCONFIG_FILE`, so Desktop's ARM-oriented
`Debug.xcconfig` cannot exclude the Intel library. The checkout, ARM runners,
physical-device builds, and macOS host-tool settings are unchanged.

The Intel host provides nested virtualization and localhost forwarding. Docker,
Compose, Buildx, Colima and Lima come from checksum-pinned release binaries via
this repository's `macos-docker` component, avoiding Homebrew source builds.
Tools, Docker authentication configuration and VM profiles stay under
`RUNNER_TEMP`; installed host tools and preferences are not edited. The VM uses
two CPUs and at most eight GiB of RAM, reserving at least six GiB for the host and
CoreSimulator. Setup has a 15-minute bound. Compose concurrency is two for both
legacy and exact-source fixtures.

The automatic lane keeps its matching x86 simulator build. The separate
`ios_ci_test.yaml` workflow stays unchanged; the default iOS matrix still uses
four runner slots. Intel has a 210-minute job provisioning window; the other
jobs remain at 120 minutes. Local and Cloud functional limits remain 60 and
20 minutes.

Runner 4 prebuilds the exact `mobile_runner_4.dart` integration target for the
selected simulator in a separate 90-minute build step. This prevents cold Pods
and Xcode compilation from consuming the functional-test deadline before any
test body starts. Its temporary architecture override remains scoped to Flutter
commands. Verbose prebuild output is retained in `mobile-local-results/prebuild.log`;
the subsequent normal test build retains expanded/verbose output and the JSON
reporter. No functional test is skipped and the application assertions are unchanged.

The additional phase is not applicable when an older Desktop revision lacks the
focused entry point and no exact server was requested; discovery writes that
reason to the job summary. An exact-server request without this entry point
fails, so it cannot claim feature coverage from the local suite alone. If the
entry point exists, missing simulator credential support is an error. Cloud
coverage can still execute after a local suite failure, but requires a successful
build/simulator setup and successful Cloud provisioning/readiness. Cancellation
stops starting new phases. The original local failure still fails the job.

By default, the fixture pulls `latest-amd64` Cloud/Worker images using the main
server repository's Compose configuration. Publish the server API in the normal
latest image before relying on that mode for new API coverage. Mutable `latest`
is not an exact-source guarantee.

Registry startup uses `start_mobile_cloud.py` with plain output and at most two
concurrent Compose operations. It retains pull/start output and attempt results
under `mobile-cloud-results/startup` before the tests begin, so the existing
always-run results upload includes a failed pull even when no test can start.
Recognized temporary transfer failures, such as unexpected EOF or connection
timeouts, receive at most three 180-second pull attempts with bounded backoff.
Authentication, missing-image, storage and unknown errors stop immediately.
Pulls, retry delays and startup share a monotonic 870-second budget within the
existing 15-minute step, leaving time for reports and image identities. Startup
uses the remaining budget and `--no-build --pull never` to consume the completed
pull. A failed or exhausted operation still fails the job;
test deadlines and assertions are unchanged. Diagnostics exclude credential
files, service logs and rendered Compose environments. Exact-source mode keeps
its existing verified startup and never falls back to this registry helper.

For pre-merge API validation, supply both `cloud_source_sha` and
`cloud_image_run_id`, with an immutable Desktop SHA in `pr_ref`. Runner 4 reuses
the [existing exact-runtime component](desktop-exact-cloud.md), including its
Desktop dependency-pin, source-checkout, private-build, image-label and running
container identity checks. It consumes an already successful private image build
from the Desktop pipeline; it never builds or publishes another server image.
Both exact inputs are required together. A missing artifact or provenance failure
fails the job; registry images cannot replace a failed exact request. The saved
runtime manifest records the Desktop, Cloud and CI revisions and image identities.

Manual `workflow_dispatch` accepts `test_runner=4` to run only the existing fourth
job, including its local suite and focused Cloud phase. Omit it or select `all`
for the normal four jobs. Repository dispatch and pull requests retain all four
jobs. To validate a CI branch before merge, select that workflow branch with
`--ref`, pass the feature's immutable Desktop SHA as `pr_ref`, and reuse its image
build with the two exact inputs. Wait for active related runs before dispatching:
the concurrency key includes `pr_number`, so another run for that PR can cancel
its current iOS run. An iOS-only dispatch does not repeat Flutter/Rust pipelines.

For runtime-stall diagnosis, manual dispatch also accepts `local_test_name`, a
test-name regular expression applied only to runner 4's local command. Use one
regex with alternation to select a predecessor and the stalled case in their
existing registration order. The input is passed as one literal argument and
the job summary marks the local coverage as partial. The target, simulator,
prebuild, assertions and Cloud suite stay the same. An empty value runs the full
local suite; repository dispatch ignores this manual diagnostic input. A filtered
success does not validate the complete local suite.

`mobile_row_cloud.py` creates a new private `.env` and a mode-0600 Dart-define
file with the same randomly generated GoTrue admin credentials. The simulator
receives its test Cloud/auth endpoints through that file; it does not depend on
inheriting the host environment. Existing files are never overwritten. External
API secrets are passed through the Compose process environment, not interpolated
into shell commands or handwritten dotenv quoting. Credential files and auth
logs are not uploaded. Cleanup removes only this job's Compose project/volumes
and generated credential files; no global Docker prune is used.

## Local checks

```sh
python3 -B -m unittest discover -s .github/scripts -p test_mobile_row_cloud.py
python3 -B -m unittest discover -s .github/scripts -p test_macos_docker.py
python3 -B -m unittest discover -s .github/scripts -p test_start_mobile_cloud.py
python3 -B -m unittest discover -s .github/scripts
python3 -B -m unittest discover -s scripts -p test_check_test_module_coverage.py
actionlint .github/workflows/ios_ci.yaml
```

The focused tests cover older/new Desktop selection, the default four-job limit
and manual single-job selection, exact source routing and refusal to fall back,
native architecture/profile alignment, local-test and fixture failure behavior, private
credential matching, refusal to overwrite files, and cleanup after a failed
configuration write. Local Compose validation checks real parser/container
metadata with dummy values; it does not execute a hosted Intel simulator. A
hosted run is still required to validate Docker startup, the selected image's API
support, and total job duration. The workflow's existing `actions-rs/toolchain@v1` obsolete
runtime diagnostic predates this change and is not suppressed in the workflow.
