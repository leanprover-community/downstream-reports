"""Per-PR downstream validation triggered by `!downstream-check`.

This subpackage holds the matrix builder, the runner-side validate
driver, the log filter, and the dispatch-level comment renderer.
``.github/workflows/mathlib-pr-validation.yml`` invokes
``validate.py`` (Python) and ``install_lakedit.sh`` (the lakedit
fetch / build wrapper); the Python modules are tested directly in
``scripts/test_pr_validation_*.py``.
"""
