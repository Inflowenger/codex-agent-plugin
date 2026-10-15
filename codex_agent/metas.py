"""The free, token-less helpers the forms call while a dialog is open.

All of these answer from the Codex app-server **without sending a token to a
model**, so they cost nothing to run. That matters: a connection test a person
presses twice should not be billable, and the Claude Agent node sets the same
expectation.

Where the Claude node needed a trick to achieve that (a transcript-only message
the model never sees), Codex gives it plainly: ``metadata``, ``account()``,
``models()`` and ``thread_list()`` are app-server calls, not turns.

A meta RPC arrives **flat** — the form's own fields, plus ``settings`` and
``value``, at the top level — and must answer with a *patch* (a dict of absolute
leaf paths), not a ``Response``. There is no error channel in the transport, so
anything that went wrong has to be said under ``formkit.NotifKey``, or the
button appears to do nothing.
"""

from __future__ import annotations

import json
import os
from typing import Any

from inflow_plugin_sdk import Meta, Request, Response, formkit
from inflow_plugin_sdk.formkit import Option

from . import forms
from .agent import codex_config
from .dirs import list_dirs
from .settings import Conn, agent_env, config_overrides, parse_settings, resolve_workspace

_HOST_KEYS = {"settings", "value", "targetField", "form"}


def decode_meta(data: bytes | None) -> dict[str, Any]:
    """Read a meta RPC's arguments, tolerating the ``{_registry, body}`` envelope
    or a bare object, and treating anything unreadable as "no arguments"."""
    if not data:
        return {}
    text = data.decode(errors="replace").strip() if isinstance(data, (bytes, bytearray)) else str(data).strip()
    if text == "":
        return {}
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(parsed, dict):
        return {}
    body = parsed.get("body")
    return body if isinstance(body, dict) else parsed


def _conn_from(body: dict[str, Any]) -> Conn:
    """A profile may arrive under ``settings`` (an action's form) or as the open
    settings dialog's own fields."""
    settings = body.get("settings")
    if isinstance(settings, dict) and settings:
        return parse_settings(settings)
    return parse_settings({k: v for k, v in body.items() if k not in _HOST_KEYS})


def _executable() -> str:
    return (os.environ.get("CODEX_EXECUTABLE") or "").strip()


def _params(conn: Conn, cwd: str):
    from .agent import RunParams

    return RunParams(
        prompt="",
        thread_id="",
        cwd=cwd,
        env=agent_env(conn),
        config_overrides=config_overrides(conn),
        codex_bin=_executable(),
    )


def _client(conn: Conn, cwd: str):
    """An unstarted client. Use it with ``async with`` — the transport starts on
    entry, and ``metadata`` is only readable once it has."""
    from openai_codex import AsyncCodex

    return AsyncCodex(codex_config(_params(conn, cwd)))


def _where(conn: Conn) -> str:
    """A directory to boot the runtime in. The probe does no work there, so the
    root (or the process's own cwd) is enough."""
    w = resolve_workspace(None, conn, os.environ.get("CODEX_WORKSPACE_ROOT") or os.getcwd())
    return w.cwd or os.getcwd()


# ------------------------------------------------------------------ probe --


async def probe(conn: Conn) -> dict[str, Any]:
    """Boot the app-server, ask it about itself, shut it down. No model call."""
    async with _client(conn, _where(conn)) as client:
        meta = client.metadata
        info: dict[str, Any] = {}
        server = getattr(meta, "serverInfo", None)
        # The version string carries the platform and the SDK after the number:
        # "0.162.1 (Linux Mint …) …". The number is the part worth showing.
        info["version"] = str(getattr(server, "version", "") or "").split(" ", 1)[0]
        info["name"] = str(getattr(server, "name", "") or "codex")
        info["platform"] = f"{getattr(meta, 'platformOs', '')}".strip()

        try:
            account = await client.account()
            acc = getattr(account, "account", None)
            info["credential"] = _account_line(acc)
            info["loggedIn"] = acc is not None
        except Exception as exc:  # noqa: BLE001
            info["credential"] = f"could not be read: {exc}"
            info["loggedIn"] = False
        return info


#: How Codex names the credential it found, in words a person reads.
_CREDENTIAL_WORDS = {"apiKey": "an API key", "chatgpt": "a ChatGPT login", "api_key": "an API key"}


def _account_line(acc: Any) -> str:
    """What the host is authenticated as.

    ``account`` is a small typed model whose fields differ by credential kind, so
    it is read from its dump rather than by guessing attribute names. Note that
    ``requires_openai_auth`` on the response is NOT a "please log in" flag — it
    is true for an API-key account that is perfectly usable — so it is not
    reported as one.
    """
    if acc is None:
        return "none — the host is not logged in and no profile key was given"
    data = acc.model_dump() if hasattr(acc, "model_dump") else dict(getattr(acc, "__dict__", {}))
    kind = str(data.get("type", "") or "")
    who = ""
    for key in ("email", "account_id", "plan"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            who = v.strip()
            break
    label = _CREDENTIAL_WORDS.get(kind, kind or "present")
    return f"{label} ({who})" if who else label


async def _close(client: Any) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------- the handlers --


async def ping(req: Request) -> Any:
    """Connection test — the runtime, its credential, the default model and the
    workspace. Also the settings form's ``submit_to``, so saving a profile tests
    it."""
    body = decode_meta(getattr(req, "data", None))
    conn = _conn_from(body)
    where = resolve_workspace(None, conn, os.environ.get("CODEX_WORKSPACE_ROOT") or "")

    try:
        info = await probe(conn)
    except Exception as exc:  # noqa: BLE001
        return formkit.failure(
            "Codex did not start on the plugin's host: %s — install it, or set CODEX_EXECUTABLE", str(exc)
        ).patch(None)

    root = where.cwd or "(unfenced — any directory)"
    if where.error and conn.workspace_root:
        root = f"invalid: {where.error}"

    credential = "the profile's API key" if conn.api_key else info.get("credential", "the host's own login")
    return formkit.success(
        "Codex %s on this host · credential: %s · workspace root: %s",
        info.get("version", "?"),
        credential,
        root,
    ).patch(None)


async def models(req: Request) -> Any:
    """The models *this host* offers; re-renders the open form with the model
    field as a drop-down."""
    body = decode_meta(getattr(req, "data", None))
    conn = _conn_from(body)
    try:
        async with _client(conn, _where(conn)) as client:
            listing = await client.models()
    except Exception as exc:  # noqa: BLE001
        return formkit.failure("could not list models: %s", str(exc)).patch(None)

    options: list[Option] = []
    for m in getattr(listing, "data", None) or []:
        value = str(getattr(m, "id", None) or getattr(m, "slug", None) or getattr(m, "model", "") or "").strip()
        if value == "":
            continue
        label = str(getattr(m, "display_name", None) or getattr(m, "name", None) or value)
        options.append(Option(value, label))

    if not options:
        return formkit.warning("this host advertised no model list; type the model name instead").patch(None)
    return formkit.choices("model", *options)


async def dirs(req: Request) -> Any:
    """The directories under the connection's workspace root, offered as rows for
    the "Also writable" list."""
    body = decode_meta(getattr(req, "data", None))
    conn = _conn_from(body)
    where = resolve_workspace(None, conn, os.environ.get("CODEX_WORKSPACE_ROOT") or "")
    if where.error:
        return formkit.failure("%s", where.error).patch(None)
    found = list_dirs(where.cwd)
    if not found:
        return formkit.info("nothing under %s yet", where.cwd).patch(None)
    return formkit.success("%d directories under %s", len(found), where.cwd).patch(
        {"additionalDirectories": found}
    )


async def threads(req: Request) -> Any:
    """The Codex threads already on this host, newest first — a way to see what
    this plugin's nodes have been doing without leaving the dialog."""
    body = decode_meta(getattr(req, "data", None))
    conn = _conn_from(body)
    try:
        async with _client(conn, _where(conn)) as client:
            listing = await client.thread_list(limit=20)
    except Exception as exc:  # noqa: BLE001
        return formkit.failure("could not list threads: %s", str(exc)).patch(None)

    rows = getattr(listing, "data", None) or getattr(listing, "threads", None) or []
    if not rows:
        return formkit.info("no Codex threads on this host yet").patch(None)
    lines = []
    for t in rows[:20]:
        tid = str(getattr(t, "id", ""))
        name = str(getattr(t, "name", "") or getattr(t, "preview", "") or "")
        lines.append(f"{tid} {name}".strip())
    return formkit.info("%d threads: %s", len(rows), "; ".join(lines[:5])).patch(None)


def metas() -> list[Meta]:
    return [
        Meta(method=forms.META_PING, request_handler=ping),
        Meta(method=forms.META_MODELS, request_handler=models),
        Meta(method=forms.META_DIRS, request_handler=dirs),
        Meta(method=forms.META_THREADS, request_handler=threads),
    ]
