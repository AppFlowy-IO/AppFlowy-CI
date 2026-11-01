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
    echo "::error::Required SCIM/MCP regression coverage is missing or ignored: $prefix"
    return 1
  fi
}

if [[ "$mode" == prepare ]]; then
  echo '==> Preparing workspace member test inventories and binaries'
  # Guard new SCIM/MCP contracts only when their production modules
  # exist, so this CI revision also supports older Cloud source refs.
  # Use the same package selection for listing and execution to avoid
  # recompiling a different feature graph just to count tests.
  scim_contract=libs/appflowy-cloud-directory/src/api/scim/user_attributes.rs
  mcp_contract=libs/appflowy-mcp-core/src/workspace_token.rs
  if [[ -f "$scim_contract" || -f "$mcp_contract" ]]; then
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
  else
    echo "==> Older Cloud source: running package tests without managed-user count guards"
  fi

  # Inventory uses --lib as before; also prepare binary and integration targets before the
  # separate execution deadline. Cargo still owns execution and doctest discovery below.
  RUST_LOG="info" DISABLE_CI_TEST_LOG="true" cargo test --locked "${member_packages[@]}" --no-run
else
  echo '==> Testing workspace member packages'
  RUST_LOG="info" DISABLE_CI_TEST_LOG="true" cargo test --locked \
    "${member_packages[@]}" -- --test-threads=1
fi
