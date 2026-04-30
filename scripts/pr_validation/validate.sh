#!/usr/bin/env bash
# Validate a single downstream against a mathlib4 PR's would-be-merged tree.
#
# Inputs (env, all required):
#   PR_NUMBER, HEAD_REPO, MERGE_SHA — identifies the PR and the merge ref
#   DOWNSTREAM, DOWNSTREAM_REPO, DEFAULT_BRANCH, DEPENDENCY_NAME
#                                  — identifies the downstream to test
#   WORKDIR                        — scratch directory for clones
#   OUTPUT_DIR                     — directory for result.json + build.log
#   TOOL_BIN                       — directory containing the lakedit binary
#
# Writes:
#   $OUTPUT_DIR/result.json — { status, stage, message, downstream, merge_sha }
#                             where status ∈ { pass, fail, infra_failure }
#   $OUTPUT_DIR/build.log   — combined log of every subprocess
#
# Exit code is 0 in all cases except a script-level error: a build failure
# is the meaningful answer, not an infra failure.

set -euo pipefail

: "${PR_NUMBER:?}"
: "${HEAD_REPO:?}"
: "${MERGE_SHA:?}"
: "${DOWNSTREAM:?}"
: "${DOWNSTREAM_REPO:?}"
: "${DEFAULT_BRANCH:?}"
: "${DEPENDENCY_NAME:?}"
: "${WORKDIR:?}"
: "${OUTPUT_DIR:?}"
: "${TOOL_BIN:?}"

mkdir -p "$WORKDIR" "$OUTPUT_DIR"
RESULT="$OUTPUT_DIR/result.json"
LOG="$OUTPUT_DIR/build.log"
: > "$LOG"

DOWNSTREAM_SHA=""

emit() {  # status, stage, message
  python3 - "$1" "$2" "$3" "$DOWNSTREAM_SHA" <<'PY'
import json, os, sys
status, stage, message, downstream_sha = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(os.environ["RESULT"], "w") as f:
    json.dump(
        {
            "status": status,
            "stage": stage,
            "message": message,
            "downstream": os.environ["DOWNSTREAM"],
            "merge_sha": os.environ["MERGE_SHA"],
            "downstream_sha": downstream_sha or None,
        },
        f,
    )
PY
}
export RESULT

# ---- 1. Clone mathlib4 at the merge SHA --------------------------------------
ML="$WORKDIR/mathlib4"
rm -rf "$ML"
if ! git clone --no-checkout "https://github.com/$HEAD_REPO.git" "$ML" \
      >> "$LOG" 2>&1; then
  emit infra_failure clone "could not clone $HEAD_REPO"
  exit 0
fi
if ! git -C "$ML" fetch origin "$MERGE_SHA" >> "$LOG" 2>&1; then
  emit infra_failure fetch \
    "merge SHA $MERGE_SHA not fetchable; PR may have conflicts"
  exit 0
fi
if ! git -C "$ML" checkout --detach "$MERGE_SHA" >> "$LOG" 2>&1; then
  emit infra_failure checkout "could not check out $MERGE_SHA"
  exit 0
fi

# ---- 2. Warm the olean cache --------------------------------------------------
( cd "$ML" && lake exe cache get ) >> "$LOG" 2>&1 || true   # best-effort

# ---- 3. Clone the downstream --------------------------------------------------
DS="$WORKDIR/downstream"
rm -rf "$DS"
if ! git clone --depth=1 --branch "$DEFAULT_BRANCH" \
      "https://github.com/$DOWNSTREAM_REPO.git" "$DS" >> "$LOG" 2>&1; then
  emit infra_failure clone_downstream "could not clone $DOWNSTREAM_REPO"
  exit 0
fi
DOWNSTREAM_SHA=$(git -C "$DS" rev-parse HEAD 2>/dev/null || true)

# ---- 4. lakedit set <dep> --path ---------------------------------------------
if ! "$TOOL_BIN/lakedit" set "$DEPENDENCY_NAME" --path "$ML" \
       --project-dir "$DS" >> "$LOG" 2>&1; then
  emit infra_failure lakedit "lakedit failed; see log"
  exit 0
fi

# ---- 5. lake update + lake build ---------------------------------------------
if ! ( cd "$DS" && lake update "$DEPENDENCY_NAME" ) >> "$LOG" 2>&1; then
  emit infra_failure lake_update "lake update failed; see log"
  exit 0
fi

if ( cd "$DS" && lake build ) >> "$LOG" 2>&1; then
  emit pass build "downstream builds against PR merge ref"
else
  emit fail build "lake build failed; see log"
fi

exit 0
