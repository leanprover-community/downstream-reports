#!/usr/bin/env bash
# stage-culprit-log.sh — find the culprit build log and copy it to a stable
# path so the workflow can upload it as a separate `culprit-log-<name>`
# artifact.
#
# hopscotch writes failing-commit logs under
# `.lake/hopscotch/logs/culprit/<sha>.log`; `copy_tool_artifacts` mirrors that
# tree under `<probe-output>/tool-state/logs/culprit/`.  Three probes can
# produce one (in priority order): the dedicated culprit re-probe (when
# skip-known-bad-bisect fired), the bisect probe, and the HEAD probe.
#
# Required env var:
#   OUTPUT_DIR  Per-downstream artifact directory (matches what the probe
#               step wrote to and the result-<name> artifact captured).
#
# Exits 0 always.  If no culprit log is found (passing run, error before
# build) no file is staged and the subsequent upload-artifact step gets
# `if-no-files-found: ignore`.

set -uo pipefail

if [ -z "${OUTPUT_DIR:-}" ]; then
  echo "Error: OUTPUT_DIR is not set." >&2
  exit 0
fi

if [ ! -d "$OUTPUT_DIR" ]; then
  echo "No output directory at $OUTPUT_DIR — nothing to stage."
  exit 0
fi

for candidate in \
  "$OUTPUT_DIR/culprit-probe/tool-state/logs/culprit" \
  "$OUTPUT_DIR/bisect/tool-state/logs/culprit" \
  "$OUTPUT_DIR/head-probe/tool-state/logs/culprit"; do
  if [ -d "$candidate" ]; then
    # Pick the first .log file deterministically (lexicographic sort is fine —
    # there is normally only one anyway).
    log=$(find "$candidate" -maxdepth 1 -name '*.log' -print | sort | head -n1)
    if [ -n "$log" ]; then
      cp "$log" "$OUTPUT_DIR/culprit.log"
      echo "Staged culprit log from $log"
      exit 0
    fi
  fi
done

echo "No culprit log found in any of the known probe locations."
