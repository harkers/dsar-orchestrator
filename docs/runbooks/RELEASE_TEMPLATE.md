# Case `<CASE_ID>` — release runbook

This is the per-engagement runbook template introduced by the
people-register-hardening project (Phase 6 Task 6). Copy this file to
`~/dsars/runbooks/<CASE_ID>-release-runbook.md` and fill the
placeholders before executing the case.

The runbook is the operator-facing single source of truth for what
gets run, in what order, with what checks. The conductor's preflights
(Phase 6 Tasks 2-4) automate enforcement of the gates documented here.

---

## Phase 0 — People-register pre-flight (MANDATORY)

Per spec §2.1 / §2.4 / §2.5 of
`2026-05-28-people-register-hardening-design-v1.md`. This phase is
prepended to every release runbook from this engagement forward.

1. **Build the register**
   ```bash
   dsar-build-people-register --case "$CASE_ID"
   ```

2. **Verify identity-set sufficiency** (NOT blanket coverage %)
   ```bash
   dsar-conductor verify --case "$CASE_ID" --check people-register
   ```
   Expected line (single-line wrap removed):
   ```
   ✓ Source strategy: <X> (confidence Y), N third-party clusters,
     1 subject cluster, 0 SubjectInDenylistError, K refs in
     manual-preprocessing-queue.
   ```

   Failure modes:
   - `✗ PeopleRegisterEmptyError: ... zero third-party clusters but
     corpus contains communicants` — likely source-strategy
     misdetection. Re-run `dsar-build-people-register` manually and
     inspect the source_strategy detection output.
   - `✗ EmptyIngestError: ... 0 refs ingested` — ingest didn't run or
     produced no register entries. Re-run ingest.
   - `✗ ExtractionQualityCatastrophicError: ... > 50% ocr_failure` —
     operator must triage extraction failures upstream before redact
     can produce a defensible pack.
   - `✗ ThreatModelMissingError` / `ThreatModelIncompleteError` — see
     step 4 below.

3. **Operator review** at the `/people-register` console route
   ```bash
   dsar-operator-console --case "$CASE_ID" --port 8089
   ```
   - Review top-50 ranked clusters (mention × distinct-doc × (1 -
     subject_confidence) per spec §1.5)
   - Review `subject_referent_candidate` clusters separately
     (subject_centricity_score > 0.7 — advisory only per spec §1.4;
     operator must explicitly approve preservation, never auto-
     suppressed)
   - Bulk-accept controller-domain emails as third-party redact via
     the bulk-accept action
   - Mark the controller's published main number as `preserve` if
     desired

4. **Threat-model** at `working/threat_model.md` must exist and
   validate per spec §2.5. Required sections (each with ≥ 30 chars
   of substantive content):
   - **Embed endpoint** — where the embed model runs; expected
     `127.0.0.1:8090` per spec §1.6 (Phase 2 §1.6 hardening). Never
     a public-facing endpoint.
   - **Isolation posture** — sparse-bundle / mount discipline;
     local-socket bindings; no shared state across engagements.
   - **Denylist scope** — per-case curation; never shared across
     engagements (cross-engagement gazetteer is a v2 opt-in per
     spec §7 #9).
   - **Per-engagement data flow** — what artefacts get written where;
     what leaves the bundle and when.
   - **Subject identifier handling** — how `data_subject.json`
     identifiers are suppressed from every redaction candidate
     source (spec §1.6 "never redact the subject from their own
     response pack").

5. **Gate confirmation**: the conductor refuses to start the run
   until:
   - `working/people_register.json` exists with ≥ 1 third-party
     cluster on a non-empty-communicant corpus
   - `working/third_party_denylist.json` validates against the
     subject (no fuzzy match — Phase 2 §1.6
     `validate_denylist_against_subject` cross-check)
   - `working/threat_model.md` sections complete

   **Override** (synthetic test cases only):
   `case_config.force_skip_people_register_reason = "<explicit text>"`
   + an audit event records the bypass with the reason.

---

## Phase 1-5 — pipeline execution

(Engagement-specific. Document the stages the conductor will run, any
per-stage operator decisions, and the expected artefact paths.)

```bash
dsar-conductor run --case "$CASE_ID"
```

---

## Phase 6 — DPO sign-off closure letter

Per existing engagement convention. The closure-letter checklist gains
a new line item from this template:

- [ ] People-register pre-flight passed (Phase 0)
- [ ] Subject-protection cross-check returned `ok` on the final
      denylist (Phase 2 §1.6)
- [ ] Threat-model artefact attached
- [ ] (existing engagement-specific items continue...)

---

## Status dashboard

The case-status dashboard (`~/dsars/runbooks/case-<CASE_ID>-status.sh`)
should include a `=== People register coverage ===` section per spec
§5. A reference implementation lives in
`docs/runbooks/status-dashboard-people-register.sh.snippet`.

Expected output shape:

```
=== People register coverage ===
  ✓ working/people_register.json present (last built ...)
  source strategy: <X> (confidence Y)
  entries: 247 persons / 412 emails / 89 phones
  data_subject: 1 cluster (X aliases)
  third_party: 246 clusters
  ✓ pre-redact gate: PASSED
```

If `people_register.json` is missing or the gate fails, the verdict
line flips to:

```
  ✗ BLOCKED — run dsar-build-people-register --case <id>
```
