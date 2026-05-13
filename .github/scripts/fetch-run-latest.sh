#!/usr/bin/env bash
# fetch-run-latest.sh — fetch the runs snapshot and look up one entry.
#
# Companion to fetch-latest.sh.  Pulls per-downstream latest regression-run
# metadata (run_id, run_url, job_url, target_commit, downstream_commit,
# outcome, episode_state, timestamps) from the public runs snapshot.
#
# Required env vars:
#   DOWNSTREAM  Downstream name key (e.g. "physlib") OR repo slug
#               (e.g. "leanprover-community/physlib"). Values containing "/"
#               are treated as repo slugs; all others as name keys.
#
# Writes to GITHUB_OUTPUT (every key is always written; empty string when
# no data is available):
#   run_id, run_url, job_id, job_url, result_artifact_name,
#   culprit_log_artifact_name, culprit_log_artifact_url,
#   reported_at, target_commit, downstream_commit, outcome, episode_state,
#   first_known_bad_commit, last_known_good_commit,
#   downstream_name, repo
#
# The failing-commit log itself is not exposed here — consumers download the
# `culprit-log-<name>` artifact via `culprit_log_artifact_url` when they need
# the contents.  Keeping arbitrary build output out of the published snapshot
# is intentional.
#
# Non-fatal: if the snapshot is unreachable (404, 5xx, network failure) or
# the downstream entry is missing, the script still exits 0 with all
# outputs emitted as empty strings.  The caller decides whether to
# degrade or abort.

set -uo pipefail

SNAPSHOT_URL='https://downstreamreports.z13.web.core.windows.net/runs/latest.json'

# ------------------------------------------------------------------
# Emit empty outputs.  Used as the failure path so that callers can
# always read steps.*.outputs.<key> without conditional guards.
# ------------------------------------------------------------------
emit_empty() {
  local reason="${1:-no data}"
  echo "Warning: $reason" >&2
  {
    echo "run_id="
    echo "run_url="
    echo "job_id="
    echo "job_url="
    echo "result_artifact_name="
    echo "culprit_log_artifact_name="
    echo "culprit_log_artifact_url="
    echo "reported_at="
    echo "target_commit="
    echo "downstream_commit="
    echo "outcome="
    echo "episode_state="
    echo "first_known_bad_commit="
    echo "last_known_good_commit="
    echo "downstream_name="
    echo "repo="
  } >> "$GITHUB_OUTPUT"
}

DOWNSTREAM_INPUT="${DOWNSTREAM:-}"
if [ -z "$DOWNSTREAM_INPUT" ]; then
  emit_empty "DOWNSTREAM env var is empty"
  exit 0
fi

echo "Fetching runs snapshot..."
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

if ! curl --retry 3 --retry-delay 5 --fail --silent --show-error \
    -o "$TMP" "$SNAPSHOT_URL"; then
  emit_empty "runs snapshot unavailable at $SNAPSHOT_URL"
  exit 0
fi

SCHEMA_VERSION=$(jq -r '.schema_version // empty' "$TMP" 2>/dev/null || true)
if [ -z "$SCHEMA_VERSION" ]; then
  emit_empty "runs snapshot missing schema_version"
  exit 0
fi
if [ "$SCHEMA_VERSION" -lt 1 ] 2>/dev/null; then
  echo "Warning: unexpected schema_version=$SCHEMA_VERSION (expected >= 1), continuing." >&2
fi

# Auto-detect lookup mode: values containing "/" are repo slugs.
if [[ "$DOWNSTREAM_INPUT" == */* ]]; then
  echo "Looking up downstream by repo slug: $DOWNSTREAM_INPUT"
  ENTRY=$(jq -c --arg repo "$DOWNSTREAM_INPUT" '
    .downstreams | to_entries[]
    | select(.value.repo == $repo)
    | {name: .key} + .value
  ' "$TMP" | head -n1)
else
  echo "Looking up downstream by name: $DOWNSTREAM_INPUT"
  ENTRY=$(jq -c --arg ds "$DOWNSTREAM_INPUT" '
    if .downstreams[$ds] then
      {name: $ds} + .downstreams[$ds]
    else empty end
  ' "$TMP")
fi

if [ -z "$ENTRY" ]; then
  emit_empty "downstream '$DOWNSTREAM_INPUT' not present in runs snapshot"
  exit 0
fi

# Extract fields (nulls become empty strings via // "").
get() { printf '%s' "$ENTRY" | jq -r --arg k "$1" '.[$k] // ""'; }

DS_NAME=$(get name)
REPO=$(get repo)
RUN_ID=$(get run_id)
RUN_URL=$(get run_url)
JOB_ID=$(get job_id)
JOB_URL=$(get job_url)
RESULT_ARTIFACT=$(get result_artifact_name)
CULPRIT_LOG_ARTIFACT_NAME=$(get culprit_log_artifact_name)
CULPRIT_LOG_ARTIFACT_URL=$(get culprit_log_artifact_url)
REPORTED_AT=$(get reported_at)
TARGET_COMMIT=$(get target_commit)
DOWNSTREAM_COMMIT=$(get downstream_commit)
OUTCOME=$(get outcome)
EPISODE_STATE=$(get episode_state)
FKB_COMMIT=$(get first_known_bad_commit)
LKG_COMMIT=$(get last_known_good_commit)

echo "Downstream:           $DS_NAME"
echo "Repo:                 $REPO"
echo "Latest run:           ${RUN_URL:-<none>}"
echo "Latest job:           ${JOB_URL:-<none>}"
echo "Culprit log artifact: ${CULPRIT_LOG_ARTIFACT_URL:-<none>}"
echo "Reported at:          ${REPORTED_AT:-<none>}"
echo "Target commit:        ${TARGET_COMMIT:-<none>}"
echo "Downstream commit:    ${DOWNSTREAM_COMMIT:-<none>}"
echo "Outcome:              ${OUTCOME:-<none>}"
echo "Episode state:        ${EPISODE_STATE:-<none>}"
echo "FKB commit:           ${FKB_COMMIT:-<none>}"
echo "LKG commit:           ${LKG_COMMIT:-<none>}"

{
  echo "run_id=$RUN_ID"
  echo "run_url=$RUN_URL"
  echo "job_id=$JOB_ID"
  echo "job_url=$JOB_URL"
  echo "result_artifact_name=$RESULT_ARTIFACT"
  echo "culprit_log_artifact_name=$CULPRIT_LOG_ARTIFACT_NAME"
  echo "culprit_log_artifact_url=$CULPRIT_LOG_ARTIFACT_URL"
  echo "reported_at=$REPORTED_AT"
  echo "target_commit=$TARGET_COMMIT"
  echo "downstream_commit=$DOWNSTREAM_COMMIT"
  echo "outcome=$OUTCOME"
  echo "episode_state=$EPISODE_STATE"
  echo "first_known_bad_commit=$FKB_COMMIT"
  echo "last_known_good_commit=$LKG_COMMIT"
  echo "downstream_name=$DS_NAME"
  echo "repo=$REPO"
} >> "$GITHUB_OUTPUT"
