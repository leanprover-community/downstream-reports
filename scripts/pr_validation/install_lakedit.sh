#!/usr/bin/env bash
set -euo pipefail

: "${HOPSCOTCH_REF:?HOPSCOTCH_REF must be set}"
: "${TOOL_BIN:?TOOL_BIN must be set}"

mkdir -p "$TOOL_BIN"

# Temporary build dir
SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT

echo "Cloning hopscotch at ref: $HOPSCOTCH_REF"

# `git clone --branch` only accepts branch and tag names, so we use the
# init + shallow-fetch pattern here so $HOPSCOTCH_REF can be any of
# branch / tag / commit SHA. GitHub's uploadpack lets us fetch arbitrary
# reachable SHAs from a public repo.
git init --quiet "$SRC"
git -C "$SRC" remote add origin https://github.com/leanprover-community/hopscotch.git
git -C "$SRC" fetch --depth=1 origin "$HOPSCOTCH_REF"
git -C "$SRC" checkout --quiet FETCH_HEAD

# Build lakedit
( cd "$SRC" && lake build lakedit )

BUILT="$SRC/.lake/build/bin/lakedit"
if [ ! -x "$BUILT" ]; then
  echo "error: expected lakedit at $BUILT after build" >&2
  exit 1
fi

cp "$BUILT" "$TOOL_BIN/lakedit"
chmod +x "$TOOL_BIN/lakedit"

echo "lakedit installed at $TOOL_BIN/lakedit (from $HOPSCOTCH_REF)"