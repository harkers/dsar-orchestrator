"""In-test stubs for the dsar-toolkit modules the orchestrator
imports lazily.

Each stub provides a minimal-shape ``core`` (or top-level) interface
matching what ``pipeline._run_<X>`` expects. The stubs write
realistic artefacts (with correct ``upstream_hash``) so the
resume cascade and audit log behave end-to-end.

Tests that exercise full-pipeline behaviour without the real toolkit
import this module via the ``install_toolkit_stubs`` fixture in
conftest.py — it monkeypatches sys.modules so the lazy imports
resolve to these stubs.
"""
