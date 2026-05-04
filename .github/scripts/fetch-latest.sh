#!/usr/bin/env bash
# fetch-latest.sh — fetch the downstream snapshot and look up a single entry.
#
# Required env vars:
#   DOWNSTREAM     Downstream name key (e.g. "physlib") OR repo slug
#                  (e.g. "leanprover-community/physlib"). Values containing "/"
#                  are treated as repo slugs; all others as name keys.
#
# Optional env vars:
#   QUERY_TYPE    Which commit field to extract. One of:
#                    last-known-good  (default) — last_known_good_commit
#                    first-known-bad            — first_known_bad_commit
#                    last-good-release          — last_good_release tag + commit
#                    latest-known               — first-known-bad if set,
#                                                 else last-known-good
#
# Writes to GITHUB_OUTPUT:
#   rev, commit, downstream_name, repo, dependency_name, resolved_query_type
#   rev                 — the human-readable ref (tag name for last-good-release, SHA otherwise)
#   commit              — commit SHA (resolved from the tag for last-good-release)
#   resolved_query_type — which type was actually used to pick the target.
#                         Mirrors QUERY_TYPE for static values; for
#                         "latest-known" it is "first-known-bad" (FKB
#                         present) or "last-known-good" (otherwise).
#
# Exits non-zero with a diagnostic message if the downstream is not found.

set -euo pipefail

SNAPSHOT_URL='https://downstreamreports.z13.web.core.windows.net/lkg/latest.json'

echo "Fetching snapshot..."
curl --retry 3 --retry-delay 5 --fail --silent --show-error \
  -o /tmp/downstream-snapshot.json "$SNAPSHOT_URL"

# Validate schema_version.
SCHEMA_VERSION=$(jq -r '.schema_version // empty' /tmp/downstream-snapshot.json)
if [ -z "$SCHEMA_VERSION" ]; then
  echo "Error: snapshot is missing schema_version field."
  exit 1
fi
if [ "$SCHEMA_VERSION" -lt 1 ] 2>/dev/null; then
  echo "Warning: unexpected schema_version=$SCHEMA_VERSION (expected >= 1), continuing."
fi

# Auto-detect lookup mode: values containing "/" are repo slugs, otherwise name keys.
if [[ "$DOWNSTREAM" == */* ]]; then
  echo "Looking up downstream by repo slug: $DOWNSTREAM"
  ENTRY=$(jq -c --arg repo "$DOWNSTREAM" '
    .downstreams | to_entries[]
    | select(.value.repo == $repo)
    | {name: .key} + .value
  ' /tmp/downstream-snapshot.json | head -n1)
else
  echo "Looking up downstream by name: $DOWNSTREAM"
  ENTRY=$(jq -c --arg ds "$DOWNSTREAM" '
    if .downstreams[$ds] then
      {name: $ds} + .downstreams[$ds]
    else empty end
  ' /tmp/downstream-snapshot.json)
fi

if [ -z "$ENTRY" ]; then
  echo "Error: could not find downstream '$DOWNSTREAM' in the snapshot."
  echo ""
  echo "Available downstream names:"
  jq -r '.downstreams | keys[]' /tmp/downstream-snapshot.json
  echo ""
  echo "Available repos:"
  jq -r '.downstreams[].repo' /tmp/downstream-snapshot.json
  exit 1
fi

DS_NAME=$(printf '%s' "$ENTRY" | jq -r '.name')
REPO=$(printf '%s' "$ENTRY"    | jq -r '.repo')
DEP_NAME=$(printf '%s' "$ENTRY" | jq -r '.dependency_name')

# Select the commit field based on QUERY_TYPE. For "latest-known", first
# check whether an FKB exists and resolve to it; otherwise fall through to
# last-known-good. RESOLVED_QUERY_TYPE reflects the *actual* selection so
# callers can branch on whether they got LKG or FKB.
INPUT_QUERY_TYPE="${QUERY_TYPE:-last-known-good}"
RESOLVED_QUERY_TYPE="$INPUT_QUERY_TYPE"

if [ "$INPUT_QUERY_TYPE" = "latest-known" ]; then
  FKB_CANDIDATE=$(printf '%s' "$ENTRY" | jq -r '.first_known_bad_commit // empty')
  if [ -n "$FKB_CANDIDATE" ]; then
    RESOLVED_QUERY_TYPE="first-known-bad"
  else
    RESOLVED_QUERY_TYPE="last-known-good"
  fi
fi

case "$RESOLVED_QUERY_TYPE" in
  first-known-bad)
    TARGET_COMMIT=$(printf '%s' "$ENTRY" | jq -r '.first_known_bad_commit // empty')
    TARGET_SHA="$TARGET_COMMIT"
    COMMIT_LABEL="FKB commit" ;;
  last-good-release)
    TARGET_COMMIT=$(printf '%s' "$ENTRY" | jq -r '.last_good_release // empty')
    TARGET_SHA=$(printf '%s' "$ENTRY" | jq -r '.last_good_release_commit // empty')
    COMMIT_LABEL="Release tag" ;;
  last-known-good|*)
    TARGET_COMMIT=$(printf '%s' "$ENTRY" | jq -r '.last_known_good_commit // empty')
    TARGET_SHA="$TARGET_COMMIT"
    COMMIT_LABEL="LKG commit" ;;
esac

echo "Downstream:      $DS_NAME"
echo "Repo:            $REPO"
echo "Dependency:      $DEP_NAME"
echo "Input query:     $INPUT_QUERY_TYPE"
echo "Resolved query:  $RESOLVED_QUERY_TYPE"
echo "$COMMIT_LABEL:    ${TARGET_COMMIT:-<none>}"

echo "rev=$TARGET_COMMIT"                       >> "$GITHUB_OUTPUT"
echo "commit=$TARGET_SHA"                       >> "$GITHUB_OUTPUT"
echo "downstream_name=$DS_NAME"                 >> "$GITHUB_OUTPUT"
echo "repo=$REPO"                               >> "$GITHUB_OUTPUT"
echo "dependency_name=$DEP_NAME"                >> "$GITHUB_OUTPUT"
echo "resolved_query_type=$RESOLVED_QUERY_TYPE" >> "$GITHUB_OUTPUT"
