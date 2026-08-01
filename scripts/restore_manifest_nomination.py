#!/usr/bin/env python3
"""Restore one dependency's manifest nomination without reformatting the manifest."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import sys


class ManifestError(ValueError):
    """Raised when a manifest does not identify one editable dependency."""


_INPUT_REV = re.compile(r'("inputRev"\s*:\s*)("(?:\\.|[^"\\])*")')


def _object_spans(text: str) -> list[tuple[int, int]]:
    """Return the spans of JSON objects, including nested objects."""

    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            stack.append(index)
        elif char == "}":
            if not stack:
                raise ManifestError("manifest has an unmatched closing brace")
            spans.append((stack.pop(), index + 1))
    if in_string or stack:
        raise ManifestError("manifest contains an unterminated string or object")
    return spans


def _package_span(text: str, dependency: str) -> tuple[int, int, dict]:
    matches: list[tuple[int, int, dict]] = []
    for start, end in _object_spans(text):
        try:
            value = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("name") == dependency:
            matches.append((start, end, value))
    if len(matches) != 1:
        raise ManifestError(
            f"manifest must contain exactly one package named {dependency!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def nomination(text: str, dependency: str) -> str:
    """Return ``dependency``'s nominated revision from a manifest."""

    json.loads(text)
    _, _, package = _package_span(text, dependency)
    input_rev = package.get("inputRev")
    if not isinstance(input_rev, str) or not input_rev:
        raise ManifestError(f"package {dependency!r} has no string inputRev")
    return input_rev


def restore_nomination(current: str, original: str, dependency: str) -> str:
    """Copy ``dependency``'s ``inputRev`` from ``original`` into ``current``."""

    current_data = json.loads(current)
    original_ref = nomination(original, dependency)
    start, end, _ = _package_span(current, dependency)
    package_text = current[start:end]
    fields = list(_INPUT_REV.finditer(package_text))
    if len(fields) != 1:
        raise ManifestError(
            f"package {dependency!r} must contain exactly one inputRev field; "
            f"found {len(fields)}"
        )
    field = fields[0]
    replacement = field.group(1) + json.dumps(original_ref)
    updated_package = package_text[: field.start()] + replacement + package_text[field.end() :]
    updated = current[:start] + updated_package + current[end:]

    expected = copy.deepcopy(current_data)
    packages = [
        package
        for package in expected.get("packages", [])
        if package.get("name") == dependency
    ]
    if len(packages) != 1:
        raise ManifestError(
            f"manifest must contain exactly one package named {dependency!r}; "
            f"found {len(packages)}"
        )
    packages[0]["inputRev"] = original_ref
    if json.loads(updated) != expected:
        raise ManifestError("restoring inputRev changed other manifest data")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("original", type=pathlib.Path)
    parser.add_argument("current", type=pathlib.Path)
    parser.add_argument("dependency")
    args = parser.parse_args()
    try:
        updated = restore_nomination(
            args.current.read_text(), args.original.read_text(), args.dependency
        )
        args.current.write_text(updated)
    except (OSError, json.JSONDecodeError, ManifestError) as exc:
        print(f"cannot restore manifest nomination: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
