#!/bin/bash

# Script to parse CI failures and generate detailed error reports
# Usage: ./parse_ci_failures.sh <workflow_name> <run_url> <github_token>

set -e

WORKFLOW_NAME=$1
RUN_URL=$2
GITHUB_TOKEN=$3

if [ -z "$WORKFLOW_NAME" ] || [ -z "$RUN_URL" ] || [ -z "$GITHUB_TOKEN" ]; then
  echo "Usage: $0 <workflow_name> <run_url> <github_token>"
  exit 1
fi

# Extract run ID from URL (format: https://github.com/AppFlowy-IO/AppFlowy-CI/actions/runs/12345)
RUN_ID=$(echo "$RUN_URL" | grep -oE '[0-9]+$')

if [ -z "$RUN_ID" ]; then
  echo "Error: Could not extract run ID from URL: $RUN_URL"
  exit 1
fi

# Fetch workflow run details to check conclusion
echo "Fetching workflow run details for run ID: $RUN_ID..." >&2
RUN_RESPONSE=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/AppFlowy-IO/AppFlowy-CI/actions/runs/$RUN_ID")

RUN_CONCLUSION=$(echo "$RUN_RESPONSE" | jq -r '.conclusion // "null"')

# If workflow succeeded, return empty (no failures to report)
if [ "$RUN_CONCLUSION" = "success" ]; then
  echo ""
  exit 0
fi

# Fetch all jobs for this workflow run
echo "Fetching jobs for run ID: $RUN_ID..." >&2
JOBS_RESPONSE=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/AppFlowy-IO/AppFlowy-CI/actions/runs/$RUN_ID/jobs?per_page=100")

# Count total jobs and failed jobs
TOTAL_JOBS=$(echo "$JOBS_RESPONSE" | jq '.jobs | length')
FAILED_JOBS=$(echo "$JOBS_RESPONSE" | jq '[.jobs[] | select(.conclusion == "failure")] | length')

echo "Found $TOTAL_JOBS jobs, $FAILED_JOBS failed" >&2

if [ "$FAILED_JOBS" -eq 0 ]; then
  # No failed jobs, but workflow failed (might be cancelled/timeout)
  echo "**$WORKFLOW_NAME**: $RUN_URL"
  echo "- Status: $RUN_CONCLUSION (no specific job failures detected)"
  echo ""
  exit 0
fi

# Start building the output
echo "**$WORKFLOW_NAME**: $RUN_URL"

# Parse each failed job
echo "$JOBS_RESPONSE" | jq -r '.jobs[] | select(.conclusion == "failure") |
  "JOB_NAME:\(.name)
JOB_ID:\(.id)
JOB_URL:\(.html_url)
JOB_RUNNER:\(.runner_name // "unknown")
---"' | while IFS= read -r line; do
  if [[ $line == JOB_NAME:* ]]; then
    JOB_NAME="${line#JOB_NAME:}"
  elif [[ $line == JOB_ID:* ]]; then
    JOB_ID="${line#JOB_ID:}"
  elif [[ $line == JOB_URL:* ]]; then
    JOB_URL="${line#JOB_URL:}"
  elif [[ $line == JOB_RUNNER:* ]]; then
    JOB_RUNNER="${line#JOB_RUNNER:}"
  elif [[ $line == "---" ]]; then
    echo "- **Job**: $JOB_NAME ($JOB_RUNNER) - [View Job]($JOB_URL)"

    # Fetch job logs to extract test failures
    echo "  Fetching logs for job ID: $JOB_ID..." >&2
    JOB_LOGS=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github.v3+json" \
      "https://api.github.com/repos/AppFlowy-IO/AppFlowy-CI/actions/jobs/$JOB_ID/logs" 2>/dev/null || echo "")

    if [ -n "$JOB_LOGS" ]; then
      # Parse Flutter test failures (looking for "❌" markers or test failure patterns)
      FLUTTER_FAILURES=$(echo "$JOB_LOGS" | grep -E "^❌|Test failed\.|══╡ EXCEPTION CAUGHT BY" | head -20 || true)

      if [ -n "$FLUTTER_FAILURES" ]; then
        echo "$FLUTTER_FAILURES" | while IFS= read -r failure_line; do
          # Clean up and format the failure line
          cleaned_line=$(echo "$failure_line" | sed 's/^[[:space:]]*//g' | head -c 200)
          if [ -n "$cleaned_line" ]; then
            # Check if it already starts with ❌, if not add it
            if [[ $cleaned_line == ❌* ]]; then
              echo "  $cleaned_line"
            else
              echo "  ❌ $cleaned_line"
            fi
          fi
        done
      else
        # Try to find other failure patterns
        GENERIC_FAILURES=$(echo "$JOB_LOGS" | grep -iE "error:|failed|failure|FAIL:" | head -10 || true)

        if [ -n "$GENERIC_FAILURES" ]; then
          echo "$GENERIC_FAILURES" | while IFS= read -r failure_line; do
            cleaned_line=$(echo "$failure_line" | sed 's/^[[:space:]]*//g' | head -c 200)
            if [ -n "$cleaned_line" ]; then
              echo "  ❌ $cleaned_line"
            fi
          done
        else
          echo "  ❌ Job failed (see logs for details)"
        fi
      fi
    else
      echo "  ❌ Job failed (logs not available)"
    fi

    echo ""
  fi
done

exit 0
