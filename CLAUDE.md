# dsar-orchestrator — operator context for Claude Code

This file is Claude's onboarding pack for working in this repo.
Mirrors the convention used in `zen-tei/CLAUDE.md` and
`dsar-toolkit/CLAUDE.md`.

## What this repo is

The orchestration / conductor layer that chains the modular
DSAR processing capabilities in
[`harkers/dsar-toolkit`](https://github.com/harkers/dsar-toolkit)
into a working case run.

Repo is **public** (no client data ever lives here). Client data
lives in per-engagement encrypted sparse bundles at
`/Volumes/<client>/` — never in this repo.

## Where things live

```
dsar-orchestrator/
├── README.md             # human-facing intro
├── CLAUDE.md             # this file
├── pyproject.toml        # depends on dsar-toolkit
├── src/
│   └── dsar_orchestrator/
│       ├── __init__.py
│       ├── pipeline.py    # pipeline.run(case, …)
│       ├── hash_chain.py  # upstream_hash compute + verify
│       ├── cli.py         # `dsar-pipeline` entry point
│       └── audit.py       # pipeline.jsonl writer
├── tests/
└── docs/
    ├── superpowers/
    │   ├── specs/         # versioned design docs (-vN.md convention)
    │   ├── plans/         # writing-plans output (unversioned)
    │   └── prompts/       # implementation hand-off briefs
    └── audit_schemas/
        └── pipeline.schema.json
```

## Authoritative design docs

Current versions:

- `docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v2.md`
  — current orchestration design covering all 7 dsar-toolkit phases.
- v1 (`-v1.md`) preserved on disk for reference.

When the spec iterates, follow the versioned-specs convention from
[`feedback_versioned_specs`](https://github.com/harkers/) (auto-memory):
`cp -v<N>.md -v<N+1>.md`, edit, prepend a row to the Version
history table, commit. Never edit a frozen `-vN.md`.

## Working with the toolkit

`dsar-orchestrator` imports from `dsar-toolkit`. Local dev:

```bash
pip install -e ~/projects/dsar-toolkit
pip install -e ~/projects/dsar-orchestrator
```

When iterating on a toolkit module + orchestrator together, both
installs are editable; changes in either repo are picked up
without reinstall.

## Dependency rules

| Direction | Allowed? |
|---|---|
| `dsar_orchestrator.*` → `dsar_pipeline.*` (toolkit) | yes |
| `dsar_orchestrator.*` → `dsar_embed.*`, `dsar_rerank.*`, etc. (toolkit modules) | yes |
| `dsar_orchestrator.*` → `dsar_clients.*` (toolkit shared primitives) | yes |
| `dsar_pipeline.*` (toolkit) → `dsar_orchestrator.*` | **never** |
| Any toolkit module → `dsar_orchestrator.*` | **never** |

This is the same one-way dependency the toolkit's `import-linter`
contracts already enforce internally. The orchestrator side will
add its own `.importlinter` config to enforce these on its tree.

## Conventions inherited from `~/projects/` workspace

Per `~/projects/CLAUDE.md`:

- GitHub identity: `harkers`. Push branch flow:
  feature branch → push to `harkers/dsar-orchestrator` → PR → merge.
- Public repo. Anything client-flavoured goes in a mounted sparse
  bundle, never here.
- Standard layout: `~/projects/<repo-name>/`. No extra nesting.

## Conventions inherited from dsar-toolkit

Same versioned-specs convention, same dependency-direction
discipline, same modular-package shape per module, same atomic
write + idempotency + `--if-exists` per CLI. See:

- `dsar-toolkit/docs/superpowers/specs/2026-05-22-zen-tei-integration-design-v4.md`
  § Architecture, § Operational semantics
- The two existing orchestration specs in this repo.

## When you're picked up by Claude Code

1. Read this file.
2. Read the latest `2026-05-22-pipeline-orchestration-design-v<N>.md`.
3. Cross-reference with the toolkit's integration spec v4.
4. Pick up whatever the open task is — currently: implement
   the orchestrator extension per the spec.
