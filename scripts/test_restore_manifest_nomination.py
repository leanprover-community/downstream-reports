import json

import pytest

from scripts.restore_manifest_nomination import ManifestError, restore_nomination


def manifest(mathlib_input: str, other_input: str = "master") -> str:
    return (
        '{"version": "1.2.0",\n "packages":\n '
        '[{"rev": "new", "name": "mathlib",\n'
        f'   "inputRev": {json.dumps(mathlib_input)}, "inherited": false}},\n'
        f'  {{"name": "other", "inputRev": {json.dumps(other_input)}}}]}}\n'
    )


def test_restores_only_named_dependency_without_reformatting():
    current = manifest("new-sha", other_input="new-sha")
    expected = current.replace('"inputRev": "new-sha"', '"inputRev": "master"', 1)
    assert restore_nomination(current, manifest("master"), "mathlib") == expected


def test_accepts_single_line_manifest():
    original = '{"packages":[{"name":"mathlib","inputRev":"main"}]}'
    current = '{"packages":[{"name":"mathlib","inputRev":"abc123"}]}'
    assert restore_nomination(current, original, "mathlib") == original


def test_rejects_duplicate_dependency():
    duplicate = (
        '{"packages": ['
        '{"name": "mathlib", "inputRev": "one"},'
        '{"name": "mathlib", "inputRev": "two"}]}'
    )
    with pytest.raises(ManifestError, match="exactly one"):
        restore_nomination(duplicate, manifest("master"), "mathlib")


def test_rejects_missing_original_nomination():
    original = '{"packages":[{"name":"mathlib"}]}'
    with pytest.raises(ManifestError, match="no string inputRev"):
        restore_nomination(manifest("new-sha"), original, "mathlib")
