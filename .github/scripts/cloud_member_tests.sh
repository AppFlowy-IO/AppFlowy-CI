#!/usr/bin/env bash
# Keep preparation and execution on the same complete package selection. Splitting -p lists
# changes Cargo feature unification; splitting only time budgets preserves the existing graph.
set -euo pipefail

case "${1:-}" in
  prepare|run) mode="$1" ;;
  *) echo 'Usage: cloud_member_tests.sh prepare|run' >&2; exit 2 ;;
esac
if [[ $# -ne 1 ]]; then
  echo 'Usage: cloud_member_tests.sh prepare|run' >&2
  exit 2
fi

member_packages=(
  -p workspace-folder
  -p collab-storage
  -p database-blob
  -p collab-access
  -p collab-indexing
  -p collab-snapshot
  -p collab-history
  -p collab-realtime
  -p collab-realtime-server
  -p indexer
  -p collab-stream
  -p database
  -p server-infra
  -p workspace-permission
  -p pdf-exporter
  -p collab
  -p collab-rt-entity
  -p appflowy-proto
  -p appflowy-notification
  -p appflowy-cloud-core
  -p appflowy-cloud-forms
  -p appflowy-cloud-workspace-support
  -p appflowy-cloud-workspace
  -p appflowy-cloud-admin
  -p appflowy-cloud-collab
  -p appflowy-cloud-workspace-access
  -p appflowy-cloud-pages
  -p appflowy-cloud-publishing
  -p appflowy-cloud-database-api
  -p appflowy-cloud-directory
  -p appflowy-cloud-history
  -p appflowy-mcp
  -p appflowy-mcp-core
)
# The automation change feed's crate (Cloud #1542). Older Cloud source refs do not have it, and
# Cargo rejects a -p that matches no package.
automation_feed_crate=libs/automation-feed/Cargo.toml
if [[ -f "$automation_feed_crate" ]]; then
  member_packages+=(-p automation-feed)
fi
# The database automation engine's crate, gated the same way.
automation_engine_crate=libs/automation-engine/Cargo.toml
if [[ -f "$automation_engine_crate" ]]; then
  # The workspace settings wire contract lives in appflowy-entity, outside the engine crate.
  member_packages+=(-p automation-engine -p appflowy-entity)
fi
# Cargo only runs a dependency's own unit and integration tests when its package is selected.
if [[ -f libs/flowy-formula/Cargo.toml ]]; then
  member_packages+=(-p flowy-formula)
fi
# The public HTTP API contract's crate, gated the same way because release/0.19.0 predates it.
# No other lane runs the engine PR's tests in it, appflowy-notification or appflowy-cloud-core
# above, or most of their other tests: the Permission contracts job runs only about 30 named
# appflowy-cloud-core tests.
openapi_crate=libs/appflowy-openapi/Cargo.toml
if [[ -f "$openapi_crate" ]]; then
  member_packages+=(-p appflowy-openapi)
fi

# Roadmap sources include the native outbound adapters; earlier Cloud refs do not.
# Run this crate separately so its explicitly service-backed ignored fixtures execute once.
outbound_contract=libs/appflowy-cloud-integrations/src/biz/automation_delivery.rs
provider_tests=(cargo test --locked -p appflowy-cloud-integrations --lib)
receipt_name=biz::automation_delivery::receipt_fixture::authenticated_rule_delivers_three_ordered_https_receipts
receipt_source=libs/appflowy-cloud-integrations/src/biz/automation_delivery/receipt_fixture.rs
receipt_runner=script/automation/test_outbound_receipts.py
receipt_args=()
if [[ -f "$receipt_source" || -f "$receipt_runner" ]]; then
  # Only this fixture requires authenticated Cloud, S3, a temporary CA and three HTTPS receivers.
  if ! grep -Fxq "TEST = \"$receipt_name\"" "$receipt_runner" ||
     ! awk -v runner="$receipt_runner" '$1 == "run:" && $2 == "python3" && $3 == runner { found=1 } END { exit !found }' \
       .github/workflows/swagger-api-tests.yml; then
    echo "::error::The private provider-receipts workflow must own $receipt_name"
    exit 1
  fi
  receipt_args=(--skip "$receipt_name")
fi

# These ignored measurements need the member lane's disposable PostgreSQL.
pg_cases=()
pg_targets=(--lib)
if [[ -f libs/automation-engine/src/dispatcher/checkout_measurement.rs ]]; then
  pg_cases+=(a_pool_free_recorder_records_what_the_service_records
             dispatcher::checkout_measurement::measure_dispatch_checkout_and_connection_hold)
  pg_targets+=(--test delivery)
fi
expired_measurement=''
dispatch_source=libs/automation-engine/src/dispatcher/tests.rs
if [[ -f "$dispatch_source" ]] && grep -Fq 'fn measure_expired_property_group_dispatch_transaction(' "$dispatch_source"; then
  expired_measurement=dispatcher::tests::measure_expired_property_group_dispatch_transaction
  pg_cases+=("$expired_measurement")
fi

require_tests() {
  local prefix="$1" minimum="$2" inventory="$3" selected ignored
  # Namespace selectors end in ::; named tests must match exactly.
  selected=$(awk -v prefix="$prefix" \
    '((prefix ~ /::$/ && index($0, prefix) == 1) || $0 == prefix ": test") && /: test$/ { n++ } END { print n+0 }' \
    "$inventory/all")
  ignored=$(awk -v prefix="$prefix" \
    '((prefix ~ /::$/ && index($0, prefix) == 1) || $0 == prefix ": test") && /: test$/ { n++ } END { print n+0 }' \
    "$inventory/ignored")
  echo "==> $prefix: $selected selected, $ignored ignored (minimum $minimum)"
  if [[ "$selected" -lt "$minimum" || "$ignored" -ne 0 ]]; then
    echo "::error::Required regression coverage is missing or ignored: $prefix"
    return 1
  fi
}

if [[ "$mode" == prepare ]]; then
  echo '==> Preparing workspace member test inventories and binaries'
  # Guard the SCIM/MCP contracts, the automation feed and the automation engine
  # only when their sources exist, so this CI revision also supports older Cloud
  # source refs. Use the same package selection for listing and execution to
  # avoid recompiling a different feature graph just to count tests.
  scim_contract=libs/appflowy-cloud-directory/src/api/scim/user_attributes.rs
  mcp_contract=libs/appflowy-mcp-core/src/workspace_token.rs
  if [[ -f "$scim_contract" || -f "$mcp_contract" || -f "$automation_feed_crate" ||
        -f "$automation_engine_crate" ]]; then
    test_inventory=$(mktemp -d)
    trap 'rm -rf "$test_inventory"' EXIT
    cargo test --locked "${member_packages[@]}" --lib \
      -- --list > "$test_inventory/all"
    cargo test --locked "${member_packages[@]}" --lib \
      -- --ignored --list > "$test_inventory/ignored"
    if [[ -f "$scim_contract" ]]; then
      require_tests 'api::scim::' 51 "$test_inventory"
      require_tests 'biz::directory::group::tests::scim_group_members_can_exceed_the_manual_group_limit' 1 "$test_inventory"
      # The dedicated self-host lane requires the gated audit rollback case.
      require_tests 'biz::directory::status::tests::' 11 "$test_inventory"
    fi
    if [[ -f "$mcp_contract" ]]; then
      require_tests 'workspace_token::tests::' 3 "$test_inventory"
    fi
    if [[ -f "$automation_feed_crate" ]]; then
      # The feed's no-duplicate-event invariant and its pass/restore interleavings.
      require_tests 'detector::tests::racing_detectors_emit_one_event' 1 "$test_inventory"
      require_tests 'detector::race_tests::' 4 "$test_inventory"
    fi
    if [[ -f "$automation_engine_crate" ]]; then
      # The engine's run-queue conformance kit, dispatcher and executor race suites. The
      # conformance suite is in -p database, which every Cloud ref has, so it is gated on the
      # engine crate too: otherwise it would fail every Cloud source that predates the engine.
      require_tests 'automation::run_queue_tests::conformance::' 12 "$test_inventory"
      # This exact case runs below with --ignored. Remove it from both counts so it
      # cannot satisfy the normal minimum or mask an ignored regression.
      if [[ -n "$expired_measurement" ]]; then
        for inventory in "$test_inventory/all" "$test_inventory/ignored"; do
          if ! grep -Fxq "$expired_measurement: test" "$inventory"; then
            echo "::error::Expected the compiled ignored PostgreSQL test $expired_measurement"
            exit 1
          fi
          awk -v dedicated="$expired_measurement: test" '$0 != dedicated' "$inventory" > "$inventory.regular"
          mv "$inventory.regular" "$inventory"
        done
      fi
      require_tests 'dispatcher::tests::' 12 "$test_inventory"
      require_tests 'executor::race_tests::' 10 "$test_inventory"
    fi
  else
    echo "==> Older Cloud source: running package tests without managed-user count guards"
  fi

  if [[ -f "$outbound_contract" ]]; then
    provider_inventory=$("${provider_tests[@]}" -- --list)
    # Libtest --skip is a substring filter. Prove it removes exactly the one ignored case,
    # and fail if a source still compiles that case without its separate owner.
    receipt_matches=$(awk -v name="$receipt_name" 'index($0, name) && /: test$/' <<<"$provider_inventory")
    if [[ ${#receipt_args[@]} -gt 0 || -n "$receipt_matches" ]]; then
      receipt_ignored=$("${provider_tests[@]}" -- --exact "$receipt_name" --ignored --list)
      if [[ ${#receipt_args[@]} -eq 0 || "$receipt_matches" != "$receipt_name: test" ]] ||
         ! grep -Fxq "$receipt_name: test" <<<"$receipt_ignored"; then
        echo "::error::The member exclusion must match exactly the separately owned ignored provider-receipt case"
        exit 1
      fi
    fi
    for required in \
      biz::automation_delivery::tests::webhook_signing_keys_are_encrypted_rotatable_and_workspace_scoped \
      biz::automation_delivery::tests::smtp_accepts_plain_text_with_the_stable_delivery_id \
      biz::integrations::native::runtime_tests::slack_delivery_requires_the_owner_workspace_and_a_current_write_grant \
      biz::integrations::native::runtime_tests::gmail_delivery_requires_the_owner_scope_and_verified_sender_identity; do
      if ! grep -Fxq "$required: test" <<<"$provider_inventory"; then
        echo "::error::Missing native automation provider fixture $required"
        exit 1
      fi
    done
  fi

  # Inventory uses --lib as before; also prepare binary and integration targets before the
  # separate execution deadline. Cargo still owns execution and doctest discovery below.
  RUST_LOG="info" DISABLE_CI_TEST_LOG="true" cargo test --locked "${member_packages[@]}" --no-run
else
  echo '==> Testing workspace member packages'
  # One failing test binary must not stop the binaries after it, or the suites below: their
  # results would be missing from CI. The lane still fails with the member run's exit status.
  member_status=0
  RUST_LOG="info" DISABLE_CI_TEST_LOG="true" cargo test --locked --no-fail-fast \
    "${member_packages[@]}" -- --test-threads=1 || member_status=$?
  if (( ${#pg_cases[@]} )); then
    # Libtest exits successfully on an empty exact selection. Require every result.
    pg_results=$(mktemp)
    trap 'rm -f "$pg_results"' EXIT
    NOTIFICATION_TEST_DATABASE_URL="${DATABASE_URL:?member PostgreSQL is required}" \
      cargo test --locked "${member_packages[@]}" "${pg_targets[@]}" -- \
        --exact --ignored "${pg_cases[@]}" --test-threads=1 --show-output | tee "$pg_results"
    for test_name in "${pg_cases[@]}"; do
      if ! grep -Fxq "test $test_name ... ok" "$pg_results"; then
        echo "::error::Missing PostgreSQL test result for $test_name"
        exit 1
      fi
    done
  fi
  if [[ -f "$outbound_contract" ]]; then
    # The integrations tests create temporary PostgreSQL tables, use only the dedicated
    # loopback Redis fixture, and receive provider traffic on their own loopback listeners.
    INTEGRATIONS_TEST_DATABASE_URL="${DATABASE_URL:?member PostgreSQL is required}" \
      INTEGRATIONS_TEST_REDIS_URL="${APPFLOWY_TEST_REDIS_URL:?member Redis is required}" \
      "${provider_tests[@]}" -- --include-ignored "${receipt_args[@]}" --test-threads=1
  fi
  exit "$member_status"
fi
