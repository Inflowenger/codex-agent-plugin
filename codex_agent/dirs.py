"""What is under the workspace root, for the "Also writable" picker.

Pure filesystem work on the plugin's own host: no Codex, no platform, no cost.
Hidden directories and the usual heavy build artefacts are skipped, because the
list exists to be read by a person choosing one.
"""

from __future__ import annotations

from pathlib import Path

_SKIP = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", "target", ".next", ".cache",
}


def list_dirs(root: str, depth: int = 2, limit: int = 200) -> list[str]:
    """Directories under ``root``, breadth-first, as absolute paths."""
    base = Path(root).resolve()
    if not base.is_dir():
        return []
    found: list[str] = []
    frontier = [(base, 0)]
    while frontier and len(found) < limit:
        current, level = frontier.pop(0)
        if level >= depth:
            continue
        try:
            entries = sorted(p for p in current.iterdir() if p.is_dir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith(".") or p.name in _SKIP:
                continue
            found.append(str(p))
            if len(found) >= limit:
                break
            frontier.append((p, level + 1))
    return found
