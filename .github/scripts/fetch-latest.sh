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
#
# Writes to GITHUB_OUTPUT:
#   rev, commit, downstream_name, repo, dependency_name
#   rev    — the human-readable ref (tag name for last-good-release, SHA otherwise)
#   commit — commit SHA (resolved from the tag for last-good-release)
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

# Select the commit field based on QUERY_TYPE.
RESOLVED_TYPE="${QUERY_TYPE:-last-known-good}"
case "$RESOLVED_TYPE" in
  first-known-bad)
    TARGET_COMMIT=$(printf '%s' "$ENTRY" | jq -r '.first_known_bad_commit // empty')
    TARGET_SHA="$TARGET_COMMIT"
    COMMIT_LABEL="FKB commit" ;;
  last-good-release)
    TARGET_COMMIT=$(printf '%s' "$ENTRY" | jq -r '.last_good_release // empty')
    TARGET_SHA=$(printf '%s' "$ENTRY" | jq -r '.last_good_release_commit // empty')
    COMMIT_LABEL="Release tag" ;;
  *)  # last-known-good (default)
    TARGET_COMMIT=$(printf '%s' "$ENTRY" | jq -r '.last_known_good_commit // empty')
    TARGET_SHA="$TARGET_COMMIT"
    COMMIT_LABEL="LKG commit" ;;
esac

echo "Downstream:   $DS_NAME"
echo "Repo:         $REPO"
echo "Dependency:   $DEP_NAME"
echo "Commit type:  $RESOLVED_TYPE"
echo "$COMMIT_LABEL: ${TARGET_COMMIT:-<none>}"

echo "rev=$TARGET_COMMIT"             >> "$GITHUB_OUTPUT"
echo "commit=$TARGET_SHA"             >> "$GITHUB_OUTPUT"
echo "downstream_name=$DS_NAME"       >> "$GITHUB_OUTPUT"
echo "repo=$REPO"                     >> "$GITHUB_OUTPUT"
echo "dependency_name=$DEP_NAME"      >> "$GITHUB_OUTPUT"
