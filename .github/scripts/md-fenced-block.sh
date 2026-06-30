#!/usr/bin/env bash
# md-fenced-block.sh — wrap stdin in a Markdown code fence that can't be closed
# early by its own content.
#
# The opening/closing fence is made one backtick longer than the longest run of
# backticks anywhere in the input (GFM lets a fence use >3 backticks, and a
# fence only closes on a run of *at least* that many). So a ``` (or longer) line
# inside the content stays literal instead of ending the block and spilling the
# rest of the PR body — the surrounding warning/footer — out of the code block.
#
# Used to embed hopscotch's verbatim `fix apply` output in PR bodies. Emits
# nothing when stdin is empty, so callers can pipe unconditionally.
set -uo pipefail

content=$(cat)
[ -z "$content" ] && exit 0

# Longest run of consecutive backticks in the content (0 when there are none).
# `grep` exits 1 on no match; swallow that so it doesn't trip the caller.
longest=$(printf '%s' "$content" | { grep -oE '`+' || true; } \
  | awk '{ if (length > m) m = length } END { print m + 0 }')

# Fence length: at least 3, otherwise one more than the longest internal run.
n=3
if [ "$longest" -ge "$n" ]; then
  n=$((longest + 1))
fi
fence=$(printf '`%.0s' $(seq 1 "$n"))

printf '%s\n%s\n%s\n' "$fence" "$content" "$fence"
