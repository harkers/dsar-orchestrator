"""Adapter layer — maps conductor's stage calls onto the toolkit's
actual interfaces.

Per spec v3 § "Toolkit divergence" → "Toolkit-module API shapes", the
toolkit ships interfaces that don't match the conductor's
`pipeline._run_<X>` expectations. Adapters bridge the gap; each one
is intended to **retire** when the toolkit ships a matching
`dsar_<module>.core.<verb>_case(case_path)` Python entry.

Each adapter:
- Lives in its own file (`adapters/<sub_stage>.py`)
- Has a docstring that names the toolkit issue (#1, #2, …) it's
  bridging + the trigger for retirement
- Writes artefacts with the same `upstream_hash` + schema/producer
  versioning the eventual toolkit module will write
- Is callable as `<module>.run_for_case(cfg)` (uniform signature)
- Is testable with an injectable HTTP client primitive

The conductor's `_run_<X>` helpers in `pipeline.py` call into these
adapters directly. When the toolkit ships, swap a single import +
delete the adapter file.
"""

__all__ = ["embed"]
