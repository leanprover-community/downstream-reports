"""Per-PR downstream validation triggered by `!downstream-check`.

This subpackage holds the matrix builder, the runner-side validate
shell driver, the log filter, and the dispatch-level comment renderer.
The shell side (``validate.sh``, ``install_lakedit.sh``) is invoked
by ``.github/workflows/mathlib-pr-validation.yml``; the Python modules
are tested directly in ``scripts/test_pr_validation_*.py``.
"""
