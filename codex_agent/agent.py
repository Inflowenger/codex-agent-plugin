"""The driver: one turn of a durable Codex thread, reported as an outcome.

Everything the Codex SDK knows is confined to this module, so the action layer
deals in outcomes ("done", "ask", "limit", "error") rather than in notifications,
approval requests and turn statuses.

Three things here are subtle enough to be worth stating up front.

**Thread identity.** Codex mints the thread id itself — unlike Claude Code, it
will not accept one we choose. ``thread_start()`` is an app-server call that
mints the id *before any turn runs*, which is what lets the action adopt it as
the inflow job id (see ``actions.py``). From then on the engine's own memory of
the node's last run (``_registry.jobId``) IS the handle on the conversation, with
no plugin-side state and nothing to lose when the plugin restarts.

**Approvals are how Codex asks.** The app-server sends the client a JSON-RPC
*request* when the agent wants to step outside its sandbox. The Python SDK routes
those to a caller-supplied ``approval_handler``; left alone, its default accepts
everything. We install our own, which either answers with the decision a person
already gave or refuses and parks the question for one — and this is why the
handler must be installed before a turn can run. If it cannot be installed, the
thread is forced to ``deny_all`` rather than running with an auto-accepting
handler: failing closed is the only safe direction for a node that may be
pointed at somebody's source tree.

**The stream is consumed once.** ``TurnHandle.run()`` is itself just a consumer
of ``stream()``, and it raises on a failed turn. This module iterates the stream
directly instead: it can report progress as it goes, enforce the node's own
budget, interrupt the turn when a person is needed, and treat a failure as an
outcome to route rather than an exception to catch.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openai_codex import AsyncCodex, CodexConfig, Sandbox
from openai_codex.models import (
    ItemCompletedNotification,
    ItemStartedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)

from .ask import (
    ACCEPT,
    APPROVAL_METHODS,
    DECLINE,
    Question,
    approval_question,
    decision_of,
    structured_question,
)

#: What a step looks like on the canvas while the turn runs.
OnStep = Callable[[str, str, int], None]

#: The form's sandbox values, mapped to the SDK's enum. The SDK validates by
#: identity, not by string, and it spells full access ``full-access`` while the
#: CLI (and this node's form, deliberately) calls it ``danger-full-access``.
_SANDBOX = {
    "read-only": Sandbox.read_only,
    "workspace-write": Sandbox.workspace_write,
    "danger-full-access": Sandbox.full_access,
    "full-access": Sandbox.full_access,
}


def sandbox_of(value: str):
    """The SDK's sandbox member for a form value, or ``None`` to leave the
    host's configured default alone."""
    return _SANDBOX.get((value or "").strip())


@dataclass
class RunParams:
    """One turn's worth of instruction."""

    #: What to send this turn. On a resume this usually carries a person's reply.
    prompt: str
    #: The thread this turn runs in. Minted by :func:`start_thread` when new.
    thread_id: str
    #: True when the thread already existed and this turn continues it.
    resuming: bool = False
    #: The human's words, when this turn exists to answer a parked decision.
    answer: str = ""
    cwd: str = ""
    #: Extra directories the agent may write in, already fenced by the caller.
    additional_directories: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    config_overrides: list[str] = field(default_factory=list)
    model: str = ""
    effort: str = ""
    sandbox: str = ""
    network_access: bool | None = None
    web_search: str = ""
    skip_git_repo_check: bool = False
    #: JSON Schema the final answer must match; its value lands on ``output``.
    output_schema: dict | None = None
    #: Route decisions to a person instead of refusing them outright.
    ask_human: bool = True
    #: The node's own ceilings, since Codex enforces neither.
    max_tokens: int = 0
    max_seconds: int = 0
    #: Path to a codex executable, when not the SDK's bundled runtime.
    codex_bin: str = ""
    developer_instructions: str = ""


@dataclass
class Stats:
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0
    context_window: int = 0
    items: int = 0
    commands: int = 0
    files_changed: int = 0
    duration_ms: int = 0
    model: str = ""


@dataclass
class Outcome:
    #: ``done`` · ``ask`` · ``limit`` · ``error``
    kind: str
    thread_id: str
    text: str = ""
    output: Any = None
    question: Question | None = None
    reason: str = ""
    message: str = ""
    stats: Stats = field(default_factory=Stats)
    #: The decision this turn delivered on a person's behalf, when it did.
    answered: str = ""


class _Parked(Exception):
    """Raised internally when the turn must end because a person is needed."""


def codex_config(params: RunParams) -> CodexConfig:
    return CodexConfig(
        codex_bin=params.codex_bin or None,
        env=params.env or None,
        config_overrides=tuple(params.config_overrides),
        cwd=params.cwd or None,
    )


def _install_approval_handler(codex: AsyncCodex, handler) -> bool:
    """Reach the sync client that owns the transport and give it our handler.

    ``AsyncCodexClient`` wraps the synchronous ``CodexClient`` by offloading to a
    thread, and only the sync client takes an ``approval_handler``. The public
    ``AsyncCodex(config)`` constructor does not plumb one through, so we set it on
    the client it built. This is the one place the plugin reaches past the SDK's
    public surface; it is isolated here, and it is checked rather than assumed —
    the caller forces ``deny_all`` when it returns ``False``.
    """
    try:
        codex._client._sync._approval_handler = handler  # noqa: SLF001
        return True
    except Exception:  # noqa: BLE001 — any shape change at all means "not installed"
        return False


async def start_thread(params: RunParams) -> tuple[AsyncCodex, Any, str]:
    """Open (or reopen) the thread and return it with its id.

    Called from the action's namer middleware for a NEW conversation, because the
    id it returns becomes the job id. The runtime is waiting while this runs, so
    it must stay quick — it is one app-server call, no model work.
    """
    codex = AsyncCodex(codex_config(params))
    thread = await _open(codex, params)
    return codex, thread, thread.id


async def _open(codex: AsyncCodex, params: RunParams) -> Any:
    kwargs: dict[str, Any] = {}
    if params.cwd:
        kwargs["cwd"] = params.cwd
    if params.model:
        kwargs["model"] = params.model
    if sandbox_of(params.sandbox) is not None:
        kwargs["sandbox"] = sandbox_of(params.sandbox)
    if params.developer_instructions:
        kwargs["developer_instructions"] = params.developer_instructions
    if params.resuming and params.thread_id:
        return await codex.thread_resume(params.thread_id, **kwargs)
    return await codex.thread_start(**kwargs)


async def run_turn(params: RunParams, on_step: OnStep | None = None, codex: AsyncCodex | None = None) -> Outcome:
    """Run one turn and report what happened.

    ``codex`` may be a client already opened by the namer middleware, so a new
    conversation does not pay to boot the runtime twice.
    """
    step = on_step or (lambda title, content, n: None)
    started = time.monotonic()
    stats = Stats(model=params.model)
    own_client = codex is None
    client = codex or AsyncCodex(codex_config(params))

    # The one decision a person already made, spendable once: the parked request
    # re-fires first on a resume. A further request in the same turn is a new
    # question, and gets parked like any other rather than inheriting an answer
    # that was never meant for it.
    pending: dict[str, Any] = {"question": None, "answer_spent": False}
    human_decision = decision_of(params.answer) if params.answer else None

    def approval_handler(method: str, raw_params: dict | None) -> dict:
        """Runs on the SDK's reader thread. It must not block and must not touch
        the event loop — it records what was asked and answers at once."""
        if method not in APPROVAL_METHODS:
            return {}
        if human_decision is not None and not pending["answer_spent"]:
            pending["answer_spent"] = True
            return {"decision": ACCEPT if human_decision == ACCEPT else DECLINE}
        if pending["question"] is None:
            pending["question"] = approval_question(method, raw_params)
        # Refuse, so the agent cannot proceed on a decision nobody made. The turn
        # is interrupted a moment later; the refusal is what keeps the gap safe.
        return {"decision": DECLINE}

    installed = _install_approval_handler(client, approval_handler)
    if not installed and params.sandbox != "read-only":
        # Fail closed: without our handler the SDK's default accepts everything.
        return Outcome(
            kind="error",
            thread_id=params.thread_id,
            message=(
                "this build of openai-codex does not expose the approval handler this "
                "plugin installs, and without it every approval would be auto-accepted; "
                "set Sandbox to read-only, or pin openai-codex to a version this plugin knows"
            ),
            stats=stats,
        )

    try:
        thread = await _open(client, params)
        thread_id = thread.id or params.thread_id
        turn_kwargs: dict[str, Any] = {}
        if params.effort:
            turn_kwargs["effort"] = params.effort
        if params.model:
            turn_kwargs["model"] = params.model
        if sandbox_of(params.sandbox) is not None:
            turn_kwargs["sandbox"] = sandbox_of(params.sandbox)
        if params.output_schema is not None:
            turn_kwargs["output_schema"] = params.output_schema

        handle = await thread.turn(params.prompt, **turn_kwargs)
        step("thread resumed" if params.resuming else "thread started", thread_id, 0)

        completed = None
        items: list[Any] = []
        limit_reason = ""

        async for note in handle.stream():
            payload = note.payload

            if isinstance(payload, ItemStartedNotification):
                title, content = _describe(payload.item)
                if title:
                    step(title, content, stats.items)
            elif isinstance(payload, ItemCompletedNotification):
                items.append(payload.item)
                stats.items += 1
                _count(payload.item, stats)
            elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                _read_usage(payload, stats)
            elif isinstance(payload, TurnCompletedNotification):
                completed = payload
                continue

            # A person is wanted: stop the turn rather than let it carry on with
            # a refusal it did not understand.
            if pending["question"] is not None and params.ask_human:
                await _interrupt(handle)
                raise _Parked()

            limit_reason = _over_budget(params, stats, started)
            if limit_reason:
                await _interrupt(handle)
                break

        stats.duration_ms = int((time.monotonic() - started) * 1000)
        text = _final_text(items)

        if limit_reason:
            return Outcome(kind="limit", thread_id=thread_id, text=text, reason=limit_reason, stats=stats)

        if completed is None:
            return Outcome(
                kind="error",
                thread_id=thread_id,
                text=text,
                message="codex ended without reporting a completed turn",
                stats=stats,
            )

        turn = completed.turn
        # ``status`` is an enum on a typed turn and a bare string on an older one.
        status = str(getattr(turn.status, "value", turn.status))

        if status == "failed":
            err = getattr(turn, "error", None)
            return Outcome(
                kind="error",
                thread_id=thread_id,
                text=text,
                message=str(getattr(err, "message", None) or "the turn failed"),
                stats=stats,
            )
        if status == "interrupted":
            return Outcome(
                kind="limit",
                thread_id=thread_id,
                text=text,
                reason="interrupted",
                stats=stats,
            )

        output = _structured(text, params.output_schema)

        # The agent itself asked for a person, through the answer schema.
        asked = structured_question(output) if params.output_schema is not None else None
        if asked is not None and params.ask_human:
            return Outcome(kind="ask", thread_id=thread_id, text=text, output=output, question=asked, stats=stats)

        return Outcome(
            kind="done",
            thread_id=thread_id,
            text=text,
            output=output,
            stats=stats,
            answered=human_decision or ("" if not params.answer else "instructions"),
        )

    except _Parked:
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        return Outcome(
            kind="ask",
            thread_id=params.thread_id,
            question=pending["question"],
            text="",
            stats=stats,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure is an outcome to route
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        return Outcome(kind="error", thread_id=params.thread_id, message=_message_of(exc), stats=stats)
    finally:
        if own_client:
            await _close(client)


async def _interrupt(handle) -> None:
    try:
        await handle.interrupt()
    except Exception:  # noqa: BLE001 — the turn may have ended on its own
        pass


async def _close(client: AsyncCodex) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass


def _over_budget(params: RunParams, stats: Stats, started: float) -> str:
    if params.max_tokens and stats.tokens_total >= params.max_tokens:
        return "token budget"
    if params.max_seconds and (time.monotonic() - started) >= params.max_seconds:
        return "time budget"
    return ""


def _read_usage(payload: Any, stats: Stats) -> None:
    usage = getattr(payload, "token_usage", None)
    if usage is None:
        return
    last = getattr(usage, "total", None) or getattr(usage, "last", None)
    stats.context_window = int(getattr(usage, "model_context_window", 0) or 0)
    if last is None:
        return
    stats.tokens_in = int(getattr(last, "input_tokens", 0) or 0)
    stats.tokens_out = int(getattr(last, "output_tokens", 0) or 0)
    stats.tokens_total = stats.tokens_in + stats.tokens_out


def _unwrap(item: Any) -> Any:
    return getattr(item, "root", item)


def _count(item: Any, stats: Stats) -> None:
    inner = _unwrap(item)
    kind = str(getattr(inner, "type", "") or "")
    if "command" in kind.lower():
        stats.commands += 1
    elif "filechange" in kind.lower().replace("_", ""):
        changes = getattr(inner, "changes", None)
        stats.files_changed += len(changes) if hasattr(changes, "__len__") else 1


def _describe(item: Any) -> tuple[str, str]:
    """One stream item as a progress frame: what it is, and the one line worth
    showing. Typed items mean this never has to guess at a tool's arguments."""
    inner = _unwrap(item)
    kind = str(getattr(inner, "type", "") or "item")
    flat = kind.lower().replace("_", "")

    if "commandexecution" in flat:
        return "running a command", _first_line(str(getattr(inner, "command", "")))
    if "filechange" in flat:
        changes = getattr(inner, "changes", None) or []
        paths = [str(getattr(c, "path", c)) for c in changes][:3]
        return "changing files", ", ".join(paths)
    if "mcptoolcall" in flat:
        return f"{getattr(inner, 'server', 'mcp')}: {getattr(inner, 'tool', '')}", ""
    if "websearch" in flat:
        return "searching the web", _first_line(str(getattr(inner, "query", "")))
    if "reasoning" in flat:
        return "thinking", _first_line(_text_of(inner))
    if "agentmessage" in flat:
        return "writing an answer", _first_line(_text_of(inner))
    if "plan" in flat:
        return "planning", ""
    return "", ""


def _text_of(inner: Any) -> str:
    for attr in ("text", "summary", "content"):
        v = getattr(inner, attr, None)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _final_text(items: list[Any]) -> str:
    """The agent's own last word for the turn."""
    for item in reversed(items):
        inner = _unwrap(item)
        if "agentmessage" in str(getattr(inner, "type", "")).lower().replace("_", ""):
            text = _text_of(inner)
            if text.strip():
                return text
    return ""


def _structured(text: str, schema: dict | None) -> Any:
    """With a schema set, the answer IS JSON — but it arrives as the agent's text,
    not in a field of its own, so it is parsed here and left alone otherwise."""
    if schema is None:
        return None
    raw = (text or "").strip()
    if raw == "":
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("\n") + 1 :] if "\n" in raw else raw
        if raw.lower().startswith("json\n"):
            raw = raw[5:]
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — a model that ignored its schema is not a crash
        return None


def _first_line(text: str, limit: int = 160) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _message_of(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__
