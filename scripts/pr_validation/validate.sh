#!/usr/bin/env bash
# Validate a single downstream against a mathlib4 PR.
#
# Two modes, selected by $MODE (default lkg):
#
#   MODE=lkg    — check out $LKG_COMMIT (the downstream's last-known-good
#                 mathlib commit, supplied by build_matrix.py from the
#                 published lkg/latest.json snapshot), cherry-pick the
#                 PR's commits onto it, build mathlib's library target as
#                 a sanity check, then build the downstream. Yields a
#                 verdict independent of current mathlib master health.
#                 PR-touched source files miss the upstream olean cache
#                 and rebuild on every run.
#
#   MODE=merge  — clone mathlib4 at $MERGE_SHA (the would-be-merged tree
#                 GitHub computed) and build the downstream against it.
#                 Faster because `lake exe cache get` for the merge
#                 commit is mostly a cache hit; the verdict is sensitive
#                 to current master health.
#
# Inputs (env, all required unless noted):
#   MODE            — "lkg" (default if empty) or "merge".
#   PR_NUMBER, MERGE_SHA — identifies the PR and its merge ref.
#   LKG_COMMIT      — required when MODE=lkg; mathlib SHA to rebase onto.
#   UPSTREAM_REPO   — owner/repo of the upstream we clone (default
#                     leanprover-community/mathlib4). The merge SHA and the
#                     PR's commits both live here as virtual refs even when
#                     the PR was opened from a fork, so we never need to
#                     clone the fork directly.
#   DOWNSTREAM, DOWNSTREAM_REPO, DEFAULT_BRANCH, DEPENDENCY_NAME
#                   — identifies the downstream to test.
#   DOWNSTREAM_REV  — (optional) git refspec to check out on the downstream:
#                     branch / tag / commit SHA. Defaults to $DEFAULT_BRANCH.
#   WORKDIR         — scratch directory for clones.
#   OUTPUT_DIR      — directory for result.json + build.log.
#   TOOL_BIN        — directory containing the lakedit binary.
#
# Writes:
#   $OUTPUT_DIR/result.json
#       { status, stage, message, downstream, merge_sha, downstream_sha,
#         mode, lkg_commit?, downstream_rev?, ... }
#       status ∈ { pass, fail, infra_failure }
#   $OUTPUT_DIR/build.log
#       Combined log of every subprocess (mirrors the live workflow log).
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

# LKG mode is the default; merge mode is opt-in per-entry via --merge-branch
# in the comment grammar (which the dispatcher maps to MODE=merge here).
MODE="${MODE:-lkg}"
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

# DOWNSTREAM_REV is the user-supplied `@<rev>` (branch / tag / commit SHA)
# from the comment; when absent we fall back to the inventory's
# default_branch. We resolve it via `fetch origin <rev>` + `checkout
# FETCH_HEAD` so SHAs work as cleanly as branches and tags.
DOWNSTREAM_REV="${DOWNSTREAM_REV:-$DEFAULT_BRANCH}"

mkdir -p "$WORKDIR" "$OUTPUT_DIR"
RESULT="$OUTPUT_DIR/result.json"
LOG="$OUTPUT_DIR/build.log"
: > "$LOG"

# Mirror every byte we emit — including all subprocess output — to the live
# workflow console AND to $LOG. Without this, the user sees the step "running"
# for tens of minutes with no idea where it is. With it, the GH log streams
# clone / cherry-pick / lake-build progress in real time, and $LOG still
# contains the full transcript for the build.log artifact and the failure tail
# rendered into PR comments by post_results.py / log_filter.read_log_tail.
exec > >(tee -a "$LOG") 2>&1

DOWNSTREAM_SHA=""
PR_BASE=""
PR_HEAD=""
N_COMMITS=""
REPLAYED_TREE_SHA=""

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

# GH Actions log-grouping. `::group::` opens a collapsible section in the
# live log; `::endgroup::` closes it. Use them around every phase so the
# step-log skim from the workflow run page is readable.
section()    { echo "::group::$1"; }
endsection() { echo "::endgroup::"; }

# Workflow annotations show up at the top of the workflow run page so the
# verdict is visible without expanding the validate step.
notice()  { echo "::notice title=$1::$2"; }
warn()    { echo "::warning title=$1::$2"; }
err_ann() { echo "::error title=$1::$2"; }

emit() {  # status, stage, message  -- writes result.json
  PR_BASE_SHA="$PR_BASE" \
  PR_HEAD_SHA="$PR_HEAD" \
  COMMITS_REPLAYED="$N_COMMITS" \
  REPLAYED_TREE_SHA="$REPLAYED_TREE_SHA" \
  DOWNSTREAM_REV_OUT="$DOWNSTREAM_REV" \
  DEFAULT_BRANCH_OUT="$DEFAULT_BRANCH" \
  FKB_COMMIT_OUT="${FKB_COMMIT:-}" \
  REQUESTED_NAME_OUT="${REQUESTED_NAME:-}" \
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
# The literal token the user typed for this entry (short name or
# `owner/repo` slug). Only recorded when it differs from the canonical
# downstream name — that keeps result.json compact for the common case
# where the user just typed the short name. post_results.py uses it to
# render the displayed entry label; prose continues to use `downstream`.
requested = os.environ.get("REQUESTED_NAME_OUT") or ""
if requested and requested != record["downstream"]:
    record["requested_name"] = requested
if mode == "lkg":
    record["lkg_commit"] = lkg_commit or None
# fkb_commit, when set, indicates the LKG snapshot records a regression
# for this downstream on current master. The comment renderer uses it to
# surface a definitive "master is currently broken for X" caveat instead
# of the hypothetical "if master is broken …" wording.
fkb = os.environ.get("FKB_COMMIT_OUT") or None
if fkb:
    record["fkb_commit"] = fkb
# Record the requested downstream rev only when it actually changes what
# we'd otherwise default to — that way the comment renderer can decide
# whether to surface the "(at @<rev>)" annotation.
rev = os.environ.get("DOWNSTREAM_REV_OUT") or ""
default_branch = os.environ.get("DEFAULT_BRANCH_OUT") or ""
if rev and rev != default_branch:
    record["downstream_rev"] = rev
# PR endpoints (resolved from MERGE_SHA's two parents). Both modes capture
# them once we've fetched the merge SHA: post_results.py uses them to render
# an explicit "what was tested" recipe and to skip the GitHub-API round-trip
# that the merge-mode comment used to make for head_sha.
pr_base = os.environ.get("PR_BASE_SHA") or None
pr_head = os.environ.get("PR_HEAD_SHA") or None
if pr_base:
    record["pr_base_sha"] = pr_base
if pr_head:
    record["pr_head_sha"] = pr_head
# commits_replayed is the number of commits in MERGE_SHA^1..MERGE_SHA^2 and
# applies to both modes — it's the PR's own commit count, independent of
# whether we actually cherry-picked them or just checked out the merge tree.
# replayed_tree_sha is LKG-only: it's the SHA produced by the cherry-pick.
n = os.environ.get("COMMITS_REPLAYED")
if n:
    record["commits_replayed"] = int(n)
if mode == "lkg":
    rep = os.environ.get("REPLAYED_TREE_SHA") or None
    if rep:
        record["replayed_tree_sha"] = rep
with open(os.environ["RESULT"], "w") as f:
    json.dump(record, f)
PY
}
export RESULT

# -----------------------------------------------------------------------------
# Header
# -----------------------------------------------------------------------------
section "Validation parameters"
echo "  PR:         #$PR_NUMBER"
echo "  Mode:       $MODE"
echo "  Upstream:   $UPSTREAM_REPO"
echo "  Merge SHA:  $MERGE_SHA"
[ "$MODE" = "lkg" ] && echo "  LKG commit: $LKG_COMMIT"
echo "  Downstream: $DOWNSTREAM"
echo "    repo:     $DOWNSTREAM_REPO"
echo "    rev:      $DOWNSTREAM_REV (default_branch: $DEFAULT_BRANCH)"
echo "    dep:      $DEPENDENCY_NAME"
endsection

# -----------------------------------------------------------------------------
# 1. Clone mathlib4
# -----------------------------------------------------------------------------
ML="$WORKDIR/mathlib4"
rm -rf "$ML"
section "Clone $UPSTREAM_REPO"
if ! git clone --no-checkout "https://github.com/$UPSTREAM_REPO.git" "$ML"; then
  endsection
  err_ann "Clone failed" "could not clone $UPSTREAM_REPO"
  emit infra_failure clone "could not clone $UPSTREAM_REPO"
  exit 0
fi
endsection

# -----------------------------------------------------------------------------
# 2. Resolve mathlib working tree
# -----------------------------------------------------------------------------
if [ "$MODE" = "merge" ]; then
  section "Fetch merge SHA $MERGE_SHA"
  if ! git -C "$ML" fetch origin "$MERGE_SHA"; then
    endsection
    err_ann "Fetch failed" "merge SHA $MERGE_SHA not fetchable; PR may have conflicts"
    emit infra_failure fetch \
      "merge SHA $MERGE_SHA not fetchable; PR may have conflicts"
    exit 0
  fi
  endsection

  # Resolve PR endpoints from the merge commit so result.json carries them
  # for the comment renderer. Best-effort: a fast-forward merge has only one
  # parent, in which case PR_HEAD stays empty.
  PR_BASE=$(git -C "$ML" rev-parse "$MERGE_SHA^1" 2>/dev/null || echo "")
  PR_HEAD=$(git -C "$ML" rev-parse "$MERGE_SHA^2" 2>/dev/null || echo "")
  if [ -n "$PR_BASE" ] && [ -n "$PR_HEAD" ] && [ "$PR_BASE" != "$PR_HEAD" ]; then
    N_COMMITS=$(git -C "$ML" rev-list --count "$PR_BASE..$PR_HEAD" 2>/dev/null || echo "")
  fi

  section "Check out merge SHA"
  if ! git -C "$ML" checkout --detach "$MERGE_SHA"; then
    endsection
    err_ann "Checkout failed" "could not check out $MERGE_SHA"
    emit infra_failure checkout "could not check out $MERGE_SHA"
    exit 0
  fi
  endsection
else
  section "Fetch merge SHA + LKG commit"
  if ! git -C "$ML" fetch origin "$MERGE_SHA" "$LKG_COMMIT"; then
    endsection
    err_ann "Fetch failed" "could not fetch MERGE_SHA $MERGE_SHA / LKG_COMMIT $LKG_COMMIT"
    emit infra_failure fetch \
      "could not fetch MERGE_SHA $MERGE_SHA / LKG_COMMIT $LKG_COMMIT"
    exit 0
  fi
  endsection

  # `refs/pull/N/merge` (and the merge_commit_sha mirror used by the dispatcher)
  # is `merge(base, head)` — its first parent is the base ref tip, the second
  # is the PR head. If GitHub fast-forwarded the merge (single parent), the PR
  # tree IS the merge SHA, so there's nothing to cherry-pick on top of LKG.
  section "Resolve PR endpoints from merge commit"
  if ! PR_BASE=$(git -C "$ML" rev-parse "$MERGE_SHA^1"); then
    endsection
    err_ann "rev-parse failed" "could not resolve $MERGE_SHA^1 (PR base)"
    emit infra_failure rev_parse \
      "could not resolve $MERGE_SHA^1 (PR base)"
    exit 0
  fi
  PR_HEAD=$(git -C "$ML" rev-parse "$MERGE_SHA^2" 2>/dev/null || echo "")
  echo "  PR base: $PR_BASE"
  echo "  PR head: ${PR_HEAD:-(fast-forward; same as merge SHA)}"
  endsection

  section "Check out LKG $LKG_COMMIT"
  if ! git -C "$ML" checkout --detach "$LKG_COMMIT"; then
    endsection
    err_ann "Checkout failed" "could not check out LKG $LKG_COMMIT"
    emit infra_failure checkout "could not check out LKG $LKG_COMMIT"
    exit 0
  fi
  endsection

  if [ -n "$PR_HEAD" ] && [ "$PR_BASE" != "$PR_HEAD" ]; then
    N_COMMITS=$(git -C "$ML" rev-list --count "$PR_BASE..$PR_HEAD")
    section "Cherry-pick $N_COMMITS PR commit(s) onto LKG"
    notice "Cherry-pick" \
      "Replaying $N_COMMITS PR commit(s) ($PR_BASE..$PR_HEAD) onto LKG ${LKG_COMMIT:0:7}"
    if ! git -C "$ML" \
           -c user.email=ci@downstream-reports.invalid -c user.name=ci \
           cherry-pick "$PR_BASE..$PR_HEAD"; then
      git -C "$ML" cherry-pick --abort || true
      endsection
      warn "Rebase conflict" \
        "PR commits do not apply on top of LKG ${LKG_COMMIT:0:7}; this PR likely depends on post-LKG mathlib changes"
      emit infra_failure rebase_conflict \
        "PR commits do not apply on top of LKG $LKG_COMMIT; this PR likely depends on post-LKG mathlib changes"
      exit 0
    fi
    REPLAYED_TREE_SHA=$(git -C "$ML" rev-parse HEAD)
    echo "  resulting tree: $REPLAYED_TREE_SHA"
    endsection
  else
    N_COMMITS=0
    REPLAYED_TREE_SHA=$LKG_COMMIT
    notice "Cherry-pick" "merge SHA is fast-forward; no commits to cherry-pick"
  fi
fi

# -----------------------------------------------------------------------------
# 3. Warm olean cache
# -----------------------------------------------------------------------------
section "lake exe cache get"
if ! ( cd "$ML" && lake exe cache get ); then
  echo "  (best-effort cache get failed; continuing — uncached files will be rebuilt)"
fi
endsection

# -----------------------------------------------------------------------------
# 4. (LKG only) Sanity-build mathlib's library
# -----------------------------------------------------------------------------
# The downstream's `lake build` will only pull in the mathlib targets it
# imports, so failures inside mathlib code can read as a downstream
# incompatibility. Building `Mathlib` (the top-level library target) first
# distinguishes "PR depends on post-LKG mathlib changes" from a genuine
# downstream break.
if [ "$MODE" = "lkg" ]; then
  section "Sanity-build mathlib library (lake build Mathlib)"
  if ! ( cd "$ML" && lake build Mathlib ); then
    endsection
    warn "Mathlib build failed at LKG" \
      "PR rebased onto LKG ${LKG_COMMIT:0:7} does not compile; the PR likely depends on post-LKG mathlib changes"
    emit infra_failure mathlib_build_at_lkg \
      "mathlib failed to build with this PR rebased onto LKG $LKG_COMMIT; the PR likely depends on post-LKG mathlib changes"
    exit 0
  fi
  endsection
fi

# -----------------------------------------------------------------------------
# 5. Clone downstream and check out the requested rev
# -----------------------------------------------------------------------------
# We resolve $DOWNSTREAM_REV via `fetch origin <rev>` + `checkout
# FETCH_HEAD` so the same path works for branches, tags, and reachable
# commit SHAs uniformly. Drops the `--depth=1` optimisation of the old
# branch-only clone, but downstream repos are small.
DS="$WORKDIR/downstream"
rm -rf "$DS"
section "Clone downstream $DOWNSTREAM_REPO @ $DOWNSTREAM_REV"
if ! git clone --no-checkout \
      "https://github.com/$DOWNSTREAM_REPO.git" "$DS"; then
  endsection
  err_ann "Clone failed" "could not clone $DOWNSTREAM_REPO"
  emit infra_failure clone_downstream "could not clone $DOWNSTREAM_REPO"
  exit 0
fi
if ! git -C "$DS" fetch origin "$DOWNSTREAM_REV"; then
  endsection
  err_ann "Fetch failed" "could not fetch $DOWNSTREAM_REV from $DOWNSTREAM_REPO"
  emit infra_failure fetch_downstream \
    "could not fetch $DOWNSTREAM_REV from $DOWNSTREAM_REPO"
  exit 0
fi
if ! git -C "$DS" checkout --detach FETCH_HEAD; then
  endsection
  err_ann "Checkout failed" "could not check out $DOWNSTREAM_REV"
  emit infra_failure checkout_downstream \
    "could not check out $DOWNSTREAM_REV from $DOWNSTREAM_REPO"
  exit 0
fi
DOWNSTREAM_SHA=$(git -C "$DS" rev-parse HEAD 2>/dev/null || true)
echo "  downstream HEAD: $DOWNSTREAM_SHA"
endsection

# -----------------------------------------------------------------------------
# 6. lakedit set
# -----------------------------------------------------------------------------
section "lakedit set $DEPENDENCY_NAME --path \$ML"
if ! "$TOOL_BIN/lakedit" set "$DEPENDENCY_NAME" --path "$ML" \
       --project-dir "$DS"; then
  endsection
  err_ann "lakedit failed" "lakedit set failed; see log"
  emit infra_failure lakedit "lakedit failed; see log"
  exit 0
fi
endsection

# -----------------------------------------------------------------------------
# 7. lake update + lake build (downstream)
# -----------------------------------------------------------------------------
section "lake update $DEPENDENCY_NAME"
if ! ( cd "$DS" && lake update "$DEPENDENCY_NAME" ); then
  endsection
  err_ann "lake update failed" "lake update failed; see log"
  emit infra_failure lake_update "lake update failed; see log"
  exit 0
fi
endsection

section "lake build (downstream)"
if ( cd "$DS" && lake build ); then
  endsection
  if [ "$MODE" = "lkg" ]; then
    notice "PASS" \
      "$DOWNSTREAM builds against this PR rebased onto LKG ${LKG_COMMIT:0:7}"
    emit pass build "downstream builds against PR rebased onto LKG"
  else
    notice "PASS" "$DOWNSTREAM builds against this PR (merge ref)"
    emit pass build "downstream builds against PR merge ref"
  fi
else
  endsection
  warn "FAIL" "$DOWNSTREAM failed to build against this PR (mode=$MODE)"
  emit fail build "lake build failed; see log"
fi

exit 0
