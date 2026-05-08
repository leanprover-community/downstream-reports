#!/usr/bin/env bash
# Validate a single downstream against a mathlib4 PR.
#
# Two modes, selected by $MODE:
#
#   MODE=merge  — clone mathlib4 at $MERGE_SHA (the would-be-merged tree
#                 GitHub computed) and build the downstream against it.
#                 This is the default: it is the historical behaviour and the
#                 cheapest path because `lake exe cache get` for the merge
#                 commit is mostly a cache hit.
#
#   MODE=lkg    — check out $LKG_COMMIT (the downstream's last-known-good
#                 mathlib commit, supplied by build_matrix.py from the
#                 published lkg/latest.json snapshot), cherry-pick the PR's
#                 commits onto it, build mathlib's library target as a
#                 sanity check, then build the downstream. Yields a verdict
#                 that's independent of current mathlib master health.
#                 Slower because PR-touched source files miss the upstream
#                 olean cache and have to be rebuilt.
#
# Inputs (env, all required unless noted):
#   MODE            — "merge" (default if empty) or "lkg".
#   PR_NUMBER, MERGE_SHA — identifies the PR and its merge ref.
#   LKG_COMMIT      — required when MODE=lkg; mathlib SHA to rebase onto.
#   UPSTREAM_REPO   — owner/repo of the upstream we clone (default
#                     leanprover-community/mathlib4). The merge SHA and the
#                     PR's commits both live here as virtual refs even when
#                     the PR was opened from a fork, so we never need to
#                     clone the fork directly.
#   DOWNSTREAM, DOWNSTREAM_REPO, DEFAULT_BRANCH, DEPENDENCY_NAME
#                   — identifies the downstream to test.
#   WORKDIR         — scratch directory for clones.
#   OUTPUT_DIR      — directory for result.json + build.log.
#   TOOL_BIN        — directory containing the lakedit binary.
#
# Writes:
#   $OUTPUT_DIR/result.json
#       { status, stage, message, downstream, merge_sha, downstream_sha,
#         mode, lkg_commit? }
#       status ∈ { pass, fail, infra_failure }
#   $OUTPUT_DIR/build.log
#       Combined log of every subprocess.
#
# Exit code is 0 in all cases except a script-level error: a build failure
# is the meaningful answer, not an infra failure.

set -euo pipefail

: "${PR_NUMBER:?}"
: "${MERGE_SHA:?}"
: "${DOWNSTREAM:?}"
: "${DOWNSTREAM_REPO:?}"
: "${DEFAULT_BRANCH:?}"
: "${DEPENDENCY_NAME:?}"
: "${WORKDIR:?}"
: "${OUTPUT_DIR:?}"
: "${TOOL_BIN:?}"

UPSTREAM_REPO="${UPSTREAM_REPO:-leanprover-community/mathlib4}"

MODE="${MODE:-merge}"
LKG_COMMIT="${LKG_COMMIT:-}"
case "$MODE" in
  merge) ;;
  lkg)
    if [ -z "$LKG_COMMIT" ]; then
      echo "::error::MODE=lkg requires LKG_COMMIT" >&2
      exit 1
    fi
    ;;
  *)
    echo "::error::unknown MODE=$MODE (expected merge|lkg)" >&2
    exit 1
    ;;
esac

mkdir -p "$WORKDIR" "$OUTPUT_DIR"
RESULT="$OUTPUT_DIR/result.json"
LOG="$OUTPUT_DIR/build.log"
: > "$LOG"

DOWNSTREAM_SHA=""

emit() {  # status, stage, message
  python3 - "$1" "$2" "$3" "$DOWNSTREAM_SHA" "$MODE" "$LKG_COMMIT" <<'PY'
import json, os, sys
status, stage, message, downstream_sha, mode, lkg_commit = sys.argv[1:7]
record = {
    "status": status,
    "stage": stage,
    "message": message,
    "downstream": os.environ["DOWNSTREAM"],
    "merge_sha": os.environ["MERGE_SHA"],
    "downstream_sha": downstream_sha or None,
    "mode": mode,
}
if mode == "lkg":
    record["lkg_commit"] = lkg_commit or None
with open(os.environ["RESULT"], "w") as f:
    json.dump(record, f)
PY
}
export RESULT

# ---- 1. Clone mathlib4 -------------------------------------------------------
ML="$WORKDIR/mathlib4"
rm -rf "$ML"
if ! git clone --no-checkout "https://github.com/$UPSTREAM_REPO.git" "$ML" \
      >> "$LOG" 2>&1; then
  emit infra_failure clone "could not clone $UPSTREAM_REPO"
  exit 0
fi

# ---- 2. Resolve mathlib working tree ----------------------------------------
# In `merge` mode we just check out the merge SHA. In `lkg` mode we additionally
# fetch the LKG commit, check it out, then cherry-pick the PR's commits on top
# (computed from the merge SHA's two parents).
if [ "$MODE" = "merge" ]; then
  if ! git -C "$ML" fetch origin "$MERGE_SHA" >> "$LOG" 2>&1; then
    emit infra_failure fetch \
      "merge SHA $MERGE_SHA not fetchable; PR may have conflicts"
    exit 0
  fi
  if ! git -C "$ML" checkout --detach "$MERGE_SHA" >> "$LOG" 2>&1; then
    emit infra_failure checkout "could not check out $MERGE_SHA"
    exit 0
  fi
else
  # Need both the merge ref (to read its parents) and the LKG commit (to
  # check out). One fetch round-trip pulling both refs.
  if ! git -C "$ML" fetch origin "$MERGE_SHA" "$LKG_COMMIT" >> "$LOG" 2>&1; then
    emit infra_failure fetch \
      "could not fetch MERGE_SHA $MERGE_SHA / LKG_COMMIT $LKG_COMMIT"
    exit 0
  fi

  # `refs/pull/N/merge` (and the merge_commit_sha mirror used by the dispatcher)
  # is `merge(base, head)` — its first parent is the base ref tip, the second
  # is the PR head. If GitHub fast-forwarded the merge (single parent), the PR
  # tree IS the merge SHA, so there's nothing to cherry-pick on top of LKG.
  if ! PR_BASE=$(git -C "$ML" rev-parse "$MERGE_SHA^1" 2>>"$LOG"); then
    emit infra_failure rev_parse \
      "could not resolve $MERGE_SHA^1 (PR base)"
    exit 0
  fi
  PR_HEAD=$(git -C "$ML" rev-parse "$MERGE_SHA^2" 2>/dev/null || echo "")

  if ! git -C "$ML" checkout --detach "$LKG_COMMIT" >> "$LOG" 2>&1; then
    emit infra_failure checkout "could not check out LKG $LKG_COMMIT"
    exit 0
  fi

  if [ -n "$PR_HEAD" ] && [ "$PR_BASE" != "$PR_HEAD" ]; then
    echo "Cherry-picking PR commits $PR_BASE..$PR_HEAD onto LKG $LKG_COMMIT" \
      >> "$LOG"
    if ! git -C "$ML" \
           -c user.email=ci@downstream-reports.invalid -c user.name=ci \
           cherry-pick "$PR_BASE..$PR_HEAD" >> "$LOG" 2>&1; then
      git -C "$ML" cherry-pick --abort >> "$LOG" 2>&1 || true
      emit infra_failure rebase_conflict \
        "PR commits do not apply on top of LKG $LKG_COMMIT; this PR likely depends on post-LKG mathlib changes"
      exit 0
    fi
  else
    echo "merge SHA $MERGE_SHA is fast-forward; no commits to cherry-pick" \
      >> "$LOG"
  fi
fi

# ---- 3. Warm the olean cache --------------------------------------------------
( cd "$ML" && lake exe cache get ) >> "$LOG" 2>&1 || true   # best-effort

# ---- 4. (LKG only) sanity-build mathlib's library ---------------------------
# The downstream's `lake build` will only pull in the mathlib targets it
# imports, so failures inside mathlib code can read as a downstream
# incompatibility. Building `Mathlib` (the top-level library target) first
# distinguishes "PR depends on post-LKG mathlib changes" from a genuine
# downstream break.
if [ "$MODE" = "lkg" ]; then
  if ! ( cd "$ML" && lake build Mathlib ) >> "$LOG" 2>&1; then
    emit infra_failure mathlib_build_at_lkg \
      "mathlib failed to build with this PR rebased onto LKG $LKG_COMMIT; the PR likely depends on post-LKG mathlib changes"
    exit 0
  fi
fi

# ---- 5. Clone the downstream --------------------------------------------------
DS="$WORKDIR/downstream"
rm -rf "$DS"
if ! git clone --depth=1 --branch "$DEFAULT_BRANCH" \
      "https://github.com/$DOWNSTREAM_REPO.git" "$DS" >> "$LOG" 2>&1; then
  emit infra_failure clone_downstream "could not clone $DOWNSTREAM_REPO"
  exit 0
fi
DOWNSTREAM_SHA=$(git -C "$DS" rev-parse HEAD 2>/dev/null || true)

# ---- 6. lakedit set <dep> --path ---------------------------------------------
if ! "$TOOL_BIN/lakedit" set "$DEPENDENCY_NAME" --path "$ML" \
       --project-dir "$DS" >> "$LOG" 2>&1; then
  emit infra_failure lakedit "lakedit failed; see log"
  exit 0
fi

# ---- 7. lake update + lake build ---------------------------------------------
if ! ( cd "$DS" && lake update "$DEPENDENCY_NAME" ) >> "$LOG" 2>&1; then
  emit infra_failure lake_update "lake update failed; see log"
  exit 0
fi

if ( cd "$DS" && lake build ) >> "$LOG" 2>&1; then
  if [ "$MODE" = "lkg" ]; then
    emit pass build "downstream builds against PR rebased onto LKG"
  else
    emit pass build "downstream builds against PR merge ref"
  fi
else
  emit fail build "lake build failed; see log"
fi

exit 0
