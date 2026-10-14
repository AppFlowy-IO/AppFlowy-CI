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

The Intel requirement follows the existing manual `ios_ci_test.yaml` approach:
[setup-docker-macos-action](https://github.com/douglascamata/setup-docker-macos-action)
supports hosted `macos-15-intel` nested virtualization and localhost forwarding.
The automatic lane uses the matching x86 simulator build rather than the manual
workflow's ARM profile. The separate `ios_ci_test.yaml` workflow stays unchanged;
the default iOS matrix still uses four runner slots.

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
