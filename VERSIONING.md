# Versioning policy

This repo follows three distinct version axes. All three coexist; bumping
one does not require bumping the others.

## 1. Package version (semver)

**Format:** `MAJOR.MINOR.PATCH`. Source of truth: `pyproject.toml`. Mirror in
`src/dsar_orchestrator/__init__.py::__version__` — must match exactly.

**Bump rules:**

| Change                                              | Bump |
|-----------------------------------------------------|------|
| Breaking change to a public Python entry, CLI flag rename/removal, removed module, schema major-bump on a stamped artefact | MAJOR |
| Additive public Python entry, additive CLI flag, additive optional config | MINOR |
| Internal change, bugfix, docs, tests, dep update with no behaviour change | PATCH |

**Pre-1.0 (0.x.y) waiver:** while `MAJOR == 0`, public API is provisional.
Bump MINOR for breaking *or* additive; bump PATCH for bugfixes. Bump to
`1.0.0` only when the conductor's public surface (adapter contract,
`dsar-conductor` CLI flags, stage DAG) is explicitly committed-to.

**Release process:**

1. Bump `pyproject.toml` and `__init__.py` in the same commit.
2. Update `CHANGELOG.md` (Keep-a-Changelog format).
3. Tag: `git tag -a v<MAJOR>.<MINOR>.<PATCH> -m "release"` then `git push --tags`.
4. Never force-push tags. To retract: tag the next patch with the revert.

## 2. Schema version (per-artefact wire format)

Every JSONL/JSON artefact this package writes that another process reads
(audit logs, `working/` artefacts, anything the cascade hashes) carries
`schema_version: "MAJOR.MINOR"` on every row (or in the root object for
JSON).

**Bump rules:**

| Change to artefact wire format                  | Bump            |
|-------------------------------------------------|-----------------|
| Rename, remove, or re-type any existing field   | MAJOR           |
| Add a new optional field; new optional row type | MINOR           |
| Same shape, different value semantics           | New artefact, not a bump |

Schema versions are **independent of package version**. A 1.5.2 package can
still write `schema_version="1.0"`.

**Where it lives in code:**

```python
# module-level constant per artefact-writing module
SCHEMA_VERSION = "1.0"
```

Current artefacts this repo stamps `schema_version` on (see
`src/dsar_orchestrator/adapters/`):

| Artefact (under `~/.dsar-audit/<case>/` or `<case>/working/`) | Producer | Current |
|---|---|---|
| `pipeline.jsonl` | `dsar_orchestrator.audit` | 1.0 |
| `module_checks.jsonl` | `dsar_orchestrator.pipeline` | 1.0 |
| `redact_verify.jsonl` | `dsar_orchestrator.module_agents` | 1.0 |
| `analysis.jsonl` | `dsar_orchestrator.log_analyser.core` | 1.0 |
| each `working/*.jsonl` produced by an adapter | `dsar_orchestrator.adapters.<stage>` | 1.0 |

When this list changes, update both this table and the canonical
filename-ownership table tracked at
[harkers/dsar-toolkit#3](https://github.com/harkers/dsar-toolkit/issues/3).

## 3. Producer version (per-module fingerprint)

Every audit row also carries `producer_version` so an artefact can be
attributed to exactly the module + version that wrote it.

**Format:** `"<dotted.module.path> <package_version>"`, e.g.
`"dsar_orchestrator.adapters.embed 0.1.0"`.

**Rule:** the `<package_version>` portion **always tracks the conductor's
`__version__`**. Bump them together — never leave a stale
`PRODUCER_VERSION` string after a package bump.

Producer version is informational: cascade resume keys off
`schema_version` for compatibility, not `producer_version`.

## Cross-repo coordination

The conductor sits above [harkers/dsar-toolkit](https://github.com/harkers/dsar-toolkit).
Coordination rules:

- When the toolkit ships a public Python entry that a conductor adapter
  was bridging (the v4 "retirement contract"): conductor MINOR bumps
  on the same release that retires the adapter.
- When the toolkit introduces a breaking change to a public entry the
  conductor depends on: coordinate via GitHub issue on the toolkit
  before merging. Land both sides' MAJOR bumps in lockstep, or behind
  a feature flag.
- The conductor's `dsar-conductor` CLI flags are the conductor's
  public surface; CLI rename/removal is a MAJOR bump.

## What does NOT count as a version bump

- Code in `tests/`, `scripts/`, or `docs/` that doesn't ship in the wheel.
- Refactors with byte-identical artefact output.
- Re-running formatters / lints.
- Commit messages, PR descriptions, CHANGELOG entries themselves.

## CHANGELOG.md

Keep-a-Changelog format. One `## [Unreleased]` section accumulates entries
between releases; the release process moves them under
`## [<version>] - <date>`.
