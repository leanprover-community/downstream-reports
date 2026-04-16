#!/usr/bin/env bash
# fetch-lkg.sh — fetch the LKG snapshot and look up a single downstream entry.
#
# Required env vars:
#   SNAPSHOT_URL   Full URL of the LKG snapshot JSON
#   DOWNSTREAM     Downstream name key (e.g. "physlib") OR repo slug
#                  (e.g. "leanprover-community/physlib"). Values containing "/"
#                  are treated as repo slugs; all others as name keys.
#
# Writes to GITHUB_OUTPUT:
#   lkg_commit, downstream_name, repo, dependency_name
#
# Exits non-zero with a diagnostic message if the downstream is not found.

set -euo pipefail

echo "Fetching LKG snapshot from $SNAPSHOT_URL"
curl --retry 3 --retry-delay 5 --fail --silent --show-error \
  -o /tmp/lkg-snapshot.json "$SNAPSHOT_URL"

# Validate schema_version.
SCHEMA_VERSION=$(jq -r '.schema_version // empty' /tmp/lkg-snapshot.json)
if [ -z "$SCHEMA_VERSION" ]; then
  echo "Error: snapshot JSON is missing schema_version field."
  exit 1
fi
if [ "$SCHEMA_VERSION" -lt 1 ] 2>/dev/null; then
  echo "Warning: unexpected schema_version=$SCHEMA_VERSION (expected >= 1), continuing."
fi

# Auto-detect lookup mode: values containing "/" are repo slugs, otherwise name keys.
if [[ "$DOWNSTREAM" == */* ]]; then
  echo "Searching snapshot by repo slug: $DOWNSTREAM"
  ENTRY=$(jq -c --arg repo "$DOWNSTREAM" '
    .downstreams | to_entries[]
    | select(.value.repo == $repo)
    | {name: .key} + .value
  ' /tmp/lkg-snapshot.json | head -n1)
else
  echo "Searching snapshot by downstream name: $DOWNSTREAM"
  ENTRY=$(jq -c --arg ds "$DOWNSTREAM" '
    if .downstreams[$ds] then
      {name: $ds} + .downstreams[$ds]
    else empty end
  ' /tmp/lkg-snapshot.json)
fi

if [ -z "$ENTRY" ]; then
  echo "Error: could not find downstream '$DOWNSTREAM' in the snapshot."
  echo ""
  echo "Available downstream names:"
  jq -r '.downstreams | keys[]' /tmp/lkg-snapshot.json
  echo ""
  echo "Available repos:"
  jq -r '.downstreams[].repo' /tmp/lkg-snapshot.json
  exit 1
fi

DS_NAME=$(printf '%s' "$ENTRY"    | jq -r '.name')
LKG_COMMIT=$(printf '%s' "$ENTRY" | jq -r '.last_known_good_commit')
REPO=$(printf '%s' "$ENTRY"       | jq -r '.repo')
DEP_NAME=$(printf '%s' "$ENTRY"   | jq -r '.dependency_name')

echo "Downstream:  $DS_NAME"
echo "Repo:        $REPO"
echo "Dependency:  $DEP_NAME"
echo "LKG commit:  $LKG_COMMIT"

echo "lkg_commit=$LKG_COMMIT"       >> "$GITHUB_OUTPUT"
echo "downstream_name=$DS_NAME"     >> "$GITHUB_OUTPUT"
echo "repo=$REPO"                   >> "$GITHUB_OUTPUT"
echo "dependency_name=$DEP_NAME"    >> "$GITHUB_OUTPUT"
