# Automatic iOS row-page Cloud coverage

`ios_ci.yaml` keeps its four existing matrix jobs. Runners 1–3 retain their ARM
macOS hosts and local suites. Runner 4 uses `macos-15-intel`, the Desktop
`development-ios-x86_64-sim` Rust profile, and an Intel iOS simulator. It runs the
existing `mobile_runner_4.dart` first, then provisions a job-owned Cloud stack on
that same host and runs `integration_test/mobile_row_page_cloud_runner.dart`.
The focused runner initializes the integration-test binding and runs the two
mobile row-page scenarios; the aggregate mobile suite is not repeated.

The Intel requirement follows the existing manual `ios_ci_test.yaml` approach:
[setup-docker-macos-action](https://github.com/douglascamata/setup-docker-macos-action)
supports hosted `macos-15-intel` nested virtualization and localhost forwarding.
The automatic lane uses the matching x86 simulator build rather than the manual
workflow's ARM profile. This change does not alter the manual workflow or add a
runner slot.

The additional phase is not applicable when an older Desktop revision lacks the
focused entry point; discovery writes that reason to the job summary. If the
entry point exists, missing simulator credential support is an error. Cloud
coverage can still execute after a local suite failure, but requires a successful
build/simulator setup and successful Cloud provisioning/readiness. Cancellation
stops starting new phases. The original local failure still fails the job.

The fixture pulls `latest-amd64` Cloud/Worker images using the main server
repository's Compose configuration. Publish the server API in the normal latest
image before enabling this runner in a Desktop revision that consumes it. This
workflow does not build or publish server images. It records image identities and
the Compose source revision; mutable `latest` is not an exact-source guarantee.

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

The focused tests cover older/new Desktop selection, the four-job limit, native
architecture/profile alignment, local-test and fixture failure behavior, private
credential matching, refusal to overwrite files, and cleanup after a failed
configuration write. Local Compose validation checks real parser/container
metadata with dummy values; it does not execute a hosted Intel simulator. A
hosted run is still required to validate Docker startup, latest-image API support,
and total job duration. The workflow's existing `actions-rs/toolchain@v1` obsolete
runtime diagnostic predates this change and is not suppressed in the workflow.
