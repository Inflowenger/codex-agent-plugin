"""The connection half of the node: what a settings profile carries, and how a
workspace path is resolved against it.

This plugin runs an agent *on its own host*, which makes it unusual among inflow
plugins: the thing a profile fences off is not an API endpoint but a directory on
disk and the credential the agent runs as. Everything here is pure — no Codex
import — so it is testable without a platform and without the runtime.

Two differences from the Claude Agent node are worth stating here, because they
change what the fence means:

* Codex sandboxes the **agent**, not just the node. ``read-only`` and
  ``workspace-write`` are enforced by the OS (Seatbelt on macOS, bubblewrap on
  Linux/WSL2), so the workspace root is a real boundary rather than organisation.
  The check in :func:`resolve_workspace` still runs first — it fails a misaimed
  node before a process is spawned, and it names the fix.
* Codex wants a git repository. ``skip_git_repo_check`` is how a node works in a
  plain directory; it is a node-level choice, not a profile one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Conn:
    """A resolved connection: the profile's values, after lenient key matching."""

    #: OpenAI API key. Empty means "use whatever the host's codex is logged in as".
    api_key: str = ""
    #: Alternate API base URL (a gateway or proxy instead of the public API).
    base_url: str = ""
    #: Default model for every action that does not name one.
    model: str = ""
    #: The one directory tree the agent may be pointed at. Empty means unfenced —
    #: the action's own workspace field is then used verbatim, which is a
    #: deliberate operator choice, not an oversight.
    workspace_root: str = ""


_SYNONYMS: dict[str, tuple[str, ...]] = {
    "api_key": ("apikey", "openaiapikey", "codexapikey", "token", "apitoken", "key", "secret"),
    "base_url": ("baseurl", "openaibaseurl", "url", "endpoint", "gateway", "host"),
    "model": ("model", "defaultmodel", "openaimodel", "codexmodel"),
    "workspace_root": ("workspaceroot", "workspace", "root", "rootdir", "basedir", "dir", "cwd"),
}


def parse_settings(raw: dict | None) -> Conn:
    """Settings profiles are often typed by hand as key/value rows, so match keys
    leniently: ignore case, spaces, dashes, underscores and dots, and accept the
    obvious synonyms. One parser, every action routed through it."""
    flat: dict[str, str] = {}
    for k, v in (raw or {}).items():
        if v is None:
            continue
        key = "".join(ch for ch in str(k).lower() if ch not in " ._-")
        val = v.strip() if isinstance(v, str) else str(v)
        if val != "":
            flat[key] = val

    def pick(names: tuple[str, ...]) -> str:
        for n in names:
            if flat.get(n):
                return flat[n]
        return ""

    return Conn(**{name: pick(names) for name, names in _SYNONYMS.items()})


def agent_env(conn: Conn, base: dict | None = None) -> dict[str, str]:
    """The environment the codex runtime subprocess runs with.

    ``CodexConfig.env`` REPLACES the subprocess environment rather than merging
    it, so the parent's PATH/HOME must be spread back in or the subprocess cannot
    even find its own helpers. A profile that carries no key leaves the key
    environment untouched, which is what lets the host's existing ``codex login``
    serve the node.
    """
    src = os.environ if base is None else base
    env = {k: str(v) for k, v in src.items() if v is not None}
    if conn.api_key:
        env["CODEX_API_KEY"] = conn.api_key
    # The plugin is a long-running service: never let the agent try to open a
    # browser or a pager on a headless host.
    env.setdefault("CI", "1")
    return env


def config_overrides(conn: Conn) -> list[str]:
    """Raw ``--config key=value`` overrides implied by the profile.

    The base URL is not an environment variable for codex — it is a config key,
    passed as a TOML literal.
    """
    out: list[str] = []
    if conn.base_url:
        out.append(f'openai_base_url="{conn.base_url}"')
    return out


def _expand(p: str) -> Path:
    """Expand a leading ``~`` and make the path absolute."""
    t = p.strip()
    if t == "~":
        return Path.home()
    if t.startswith("~/"):
        return (Path.home() / t[2:]).resolve()
    return Path(t).resolve()


def contains(root: str | Path, child: str | Path) -> bool:
    """True when ``child`` is ``root`` or sits inside it.

    No ``..`` escapes and no prefix tricks: ``/srv/ws-evil`` is not inside
    ``/srv/ws``.
    """
    r = Path(root).resolve()
    c = Path(child).resolve()
    return c == r or r in c.parents


@dataclass(frozen=True)
class Workspace:
    cwd: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == ""


def resolve_workspace(requested: str | None, conn: Conn, fallback_root: str | None = None) -> Workspace:
    """Where this run's agent works.

    Errors name the fix rather than the symptom, because they are rendered on the
    node for whoever is building the flow — not for whoever deployed the plugin.
    """
    if fallback_root is None:
        fallback_root = os.environ.get("CODEX_WORKSPACE_ROOT", "")
    root = conn.workspace_root or fallback_root
    want = (requested or "").strip()

    if not want and not root:
        return Workspace(
            error=(
                "no workspace: set Workspace on the node, or a workspace root in the "
                "settings profile (or CODEX_WORKSPACE_ROOT on the plugin's host)"
            )
        )

    cwd = _expand(want) if want else _expand(root)

    if root and not contains(_expand(root), cwd):
        return Workspace(
            error=(
                f"workspace {cwd} is outside the allowed root {_expand(root)}: point the "
                "node inside that tree, or widen the root in the settings profile"
            )
        )
    if not cwd.exists():
        return Workspace(error=f"workspace {cwd} does not exist on the plugin's host ({_host_name()})")
    if not cwd.is_dir():
        return Workspace(error=f"workspace {cwd} is not a directory")
    return Workspace(cwd=str(cwd))


def is_git_repo(path: str | Path) -> bool:
    """Whether a directory is inside a git work tree.

    Codex refuses to run outside one unless the node says to skip the check, so
    the form can explain itself instead of letting the runtime fail.
    """
    p = Path(path).resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return True
    return False


def _host_name() -> str:
    return os.environ.get("HOSTNAME") or os.environ.get("HOST") or "this host"
