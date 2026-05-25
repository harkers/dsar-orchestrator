"""Shared helper for building subprocess env dicts (issue #15).

The conductor's adapters shell out to toolkit CLIs (`dsar-redact`,
`dsar-bake`, etc.). On hosts where the toolkit is ALSO installed via
homebrew/system pip (creating shims in /opt/homebrew/bin/), PATH
resolution at subprocess time may pick the stale system shim instead
of the venv-installed copy. Bug fixes in the venv toolkit get silently
shadowed.

Fix: prepend the running interpreter's `bin/` directory to PATH inside
the subprocess env. Same pattern that `poetry`, `hatch`, and Python
venv activation use. Leaf module — imports stdlib only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def build_subprocess_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with the running interpreter's
    ``bin/`` directory prepended to ``PATH``.

    Idempotent: if ``sys.executable``'s bin is already first in PATH,
    no change is made (avoids unbounded growth from repeated calls in
    test loops).
    """
    env = dict(os.environ)
    interp_bin = str(Path(sys.executable).parent)
    existing_path = env.get("PATH", "")
    parts = existing_path.split(os.pathsep) if existing_path else []
    if parts and parts[0] == interp_bin:
        return env  # already at front; idempotent
    env["PATH"] = interp_bin + (os.pathsep + existing_path if existing_path else "")
    return env
