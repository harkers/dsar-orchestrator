"""In-test stubs for the dsar-toolkit modules the orchestrator
imports lazily.

Each stub provides a minimal-shape interface matching what the
corresponding adapter or remaining lazy-import expects. Adapters
that subprocess to a toolkit CLI (ingest, detect_2_1_to_2_4,
scope_classify, redact, export) do NOT have stub entries here — the
integration fixtures monkeypatch their ``_default_runner`` directly.

Tests that exercise full-pipeline behaviour without the real toolkit
load ``all_stubs()`` and install the dict in ``sys.modules`` from
their own fixture (see ``tests/integration/*.py``).
"""
