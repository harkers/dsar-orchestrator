"""Synthetic-case generator for DSAR pipeline testing.

Produces a deterministic 100-doc (or N-doc) case directory under a
chosen output path. Used by:

- Operators wanting a realistic test case for the pipeline
  (``dsar-synthesize-case --case-no 800001``)
- Integration tests in this repo that drive the full pipeline against
  a known-shape input

The generator is **purely synthetic** — fake names, fake emails, fake
salaries. No real PII ever enters the corpus. Also writes a
``synthetic_truth.json`` answer key recording the ground-truth class
of every generated doc (gold / mid / decoy / off_topic), which the
integration tests use to assert pipeline correctness.
"""

from dsar_orchestrator.synthesis.case import (
    DEFAULT_DOC_COUNT,
    SyntheticCase,
    synthesize_case,
)

__all__ = ["DEFAULT_DOC_COUNT", "SyntheticCase", "synthesize_case"]
