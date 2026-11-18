#!/usr/bin/env bash
# The commercial matrix starts a real self-hosted Cloud with database automations disabled.
set -euo pipefail

if [[ ! -f tests/automation/feature_gate_test.rs ]]; then
  echo 'Older Cloud source has no disabled automation HTTP contract.'
  exit 0
fi
if [[ "${APPFLOWY_DATABASE_AUTOMATIONS_ENABLED:-}" != false ]]; then
  echo '::error::The commercial automation HTTP contract requires the disabled server fixture.'
  exit 1
fi

test_name=automation::feature_gate_test::disabled_server_rejects_all_automation_operations
command=(cargo test --locked -p appflowy-cloud --features=commercial-ci --test main "$test_name")
inventory=$("${command[@]}" -- --ignored --exact --list)
if ! grep -Fxq "$test_name: test" <<<"$inventory"; then
  echo "::error::Missing ignored commercial HTTP contract: $test_name"
  exit 1
fi
"${command[@]}" -- --ignored --exact --test-threads=1
