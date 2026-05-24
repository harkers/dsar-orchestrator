"""Contract B enforcement — AST-walk conductor sources for every
`dsar_*` lazy-import string, assert each resolves against the installed
toolkit. Catches the next #1/#10/#11-class drift the moment it appears.

Gated behind `@pytest.mark.needs_toolkit` because verification requires
the real toolkit installed. CI default doesn't select this marker.

See VERSIONING.md §4 for the Contract B principle this test enforces.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

CONDUCTOR_SRC = Path(__file__).parent.parent / "src" / "dsar_orchestrator"


def _is_lazy_import_call(node: ast.Call) -> bool:
    """True if node is `_lazy_import("dsar_*")` or
    `importlib.import_module("dsar_*")`."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "_lazy_import":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "import_module":
        return True
    return False


def _collect_dsar_lazy_imports() -> set[str]:
    """Walk every src/dsar_orchestrator/*.py; collect every literal-string
    first-arg to _lazy_import / importlib.import_module that starts with
    `dsar_`."""
    targets: set[str] = set()
    for py_file in CONDUCTOR_SRC.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_lazy_import_call(node):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("dsar_"):
                    targets.add(arg.value)
    return targets


@pytest.mark.needs_toolkit
def test_contract_b_no_fictional_toolkit_modules() -> None:
    """Contract B: every `dsar_*` module the conductor lazy-imports must
    resolve against the installed toolkit. See VERSIONING.md §4."""
    targets = _collect_dsar_lazy_imports()
    assert targets, (
        "AST walker found zero `dsar_*` lazy-imports — either the conductor "
        "has stopped using lazy-import or the AST walker is broken."
    )
    missing = sorted(t for t in targets if importlib.util.find_spec(t) is None)
    assert not missing, (
        f"Contract B violated: conductor lazy-imports modules that don't "
        f"exist in the installed toolkit: {missing}. "
        f"Either fix the conductor adapter or install the missing toolkit "
        f"module. See VERSIONING.md §4 (Toolkit-coupling contract)."
    )


def test_contract_b_collector_is_not_silently_broken() -> None:
    """Sanity check on the AST walker itself — runs without `needs_toolkit`
    so default CI catches walker regressions even when toolkit absent."""
    targets = _collect_dsar_lazy_imports()
    # At minimum, the adapters/embed.py + adapters/rerank.py lazy-imports
    # should be found. Don't assert exact set (drifts with each adapter
    # added) — just that the walker finds something in dsar_clients.
    assert any(t.startswith("dsar_clients") for t in targets), (
        f"AST walker should find dsar_clients.* lazy-imports; got: {targets}"
    )
