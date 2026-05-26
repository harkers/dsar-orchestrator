"""Local-broker tools for the conductor.

A small set of OpenAI-compat client utilities that route through the
operator's local LLM broker (typically ``mlx-broker`` at
``http://127.0.0.1:8090``) rather than a cloud provider. Used for
operator-gate decisions, DSAR-approver release-readiness review, and the
per-engagement bypass passes that exist because the dsar-toolkit's
default gate routing assumes a cloud LLM (see
``docs/durant-test.md`` section 7).

Each tool accepts a ``--case-dir`` argument and writes its outputs
under that directory so the conductor's per-engagement isolation rule
(audit data lives in the encrypted bundle, code lives in the conductor
repo) is preserved by construction.
"""
