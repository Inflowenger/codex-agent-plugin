"""``{{$.path}}`` in a free-text field, resolved from the run's own context.

A prompt is only useful if it can carry what the flow already knows — the ticket,
the diff, the findings an upstream node produced. The flow's context is readable
by JSON path from inside a job, so any ``{{ ... }}`` holding a path is substituted
before the prompt is sent. ``$this`` works too: it is inflow's root for the
location this run was handed, which is what a node scoped over a list stands on.

A path that cannot be read leaves its token in place rather than silently
emptying it, so a typo is visible in the prompt the node reports instead of
becoming a blank instruction. The per-call cache fetches each distinct path once,
however many fields reference it.

Adapted from the proven implementation in the github-oc plugin.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

# {{ $.a.b }} — capture the JSON path inside the mustaches.
_VAR_RE = re.compile(r"\{\{\s*(\$[^}]+?)\s*\}\}")


class ScopeReader(Protocol):
    """Anything that can read the flow's context by path (i.e. a Job)."""

    async def cmd_get_scope(self, json_path: str) -> Any: ...


class Resolver:
    """Resolves tokens for one run, caching each path it reads."""

    def __init__(self, job: ScopeReader) -> None:
        self._job = job
        self._cache: dict[str, str] = {}

    async def resolve(self, text: str) -> str:
        if not text or "{{" not in text:
            return text
        out: list[str] = []
        last = 0
        for m in _VAR_RE.finditer(text):
            out.append(text[last : m.start()])
            path = m.group(1).strip()
            if path not in self._cache:
                self._cache[path] = await self._fetch(path)
            out.append(self._cache[path])
            last = m.end()
        out.append(text[last:])
        return "".join(out)

    async def resolve_rows(self, rows: list[str] | None) -> list[str]:
        """Resolve a list field, dropping rows that were blank to begin with."""
        return [await self.resolve(r) for r in (rows or []) if str(r).strip() != ""]

    async def _fetch(self, json_path: str) -> str:
        raw = await self._job.cmd_get_scope(json_path)
        if isinstance(raw, Exception):
            return _verbatim(json_path)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) == 0:
            return _verbatim(json_path)
        try:
            value = json.loads(raw.decode())
        except Exception:
            return raw.decode(errors="replace")
        if isinstance(value, str):
            return value
        if value is None:
            return _verbatim(json_path)
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


async def read_scope(job: ScopeReader, json_path: str) -> Any:
    """Read one path and return the parsed value, or ``None`` when unreadable.

    Job commands *return* their error rather than raising (the SDK's Go-style
    contract), so an ``Exception`` instance is a perfectly ordinary answer here
    and means "nothing to read".
    """
    raw = await job.cmd_get_scope(json_path)
    if isinstance(raw, Exception):
        return None
    if not isinstance(raw, (bytes, bytearray)) or len(raw) == 0:
        return None
    text = raw.decode(errors="replace").strip()
    if text == "":
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def _verbatim(json_path: str) -> str:
    return "{{" + json_path + "}}"
