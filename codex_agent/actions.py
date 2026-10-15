"""The action: one run of the node is one turn of a durable Codex thread.

The whole design rests on one fact about the platform — the engine hands a node
back the jobId of its own previous run over the same context
(``_registry.jobId``) — and one fact about Codex: it mints the thread id itself,
but ``thread_start()`` mints it *before any turn runs*.

Put together, those give the node the same zero-state design the Claude Agent
node has, from the other direction. Claude lets you pin the session to the job
id; Codex does not, so the job takes the *thread's* id instead, in a namer
middleware that runs before the job is accepted:

    run 1   namer opens a thread → job id IS thread id A · Codex asks ·
            port "Needs a human"
            ↓  (the flow parks at a Human in the Loop node; nothing is held open)
    run 2   _registry.jobId = A → the namer reattaches to A, the turn carries the
            person's reply, Codex continues with its full context · port "Answer"

Nothing is stored here, nothing is kept in memory between runs, and a plugin
restart in the gap changes nothing. The one name serves both systems: it is
``job.job_id``, every command subject, the stop signal's id, and the next run's
``_registry.jobId``.

One consequence worth stating: ``{{$.path}}`` tokens cannot be resolved in the
namer, because reading the flow's context needs a job id and the namer is what
decides it. So the thread is opened at the connection's root and each *turn*
names the resolved working directory — which the SDK supports as a turn option.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

from inflow_plugin_sdk import (
    Action,
    Frame,
    Icon,
    Job,
    JobContext,
    cast_request_to,
    jobstop,
    use,
    with_job_id_context,
)

from . import forms
from .agent import Outcome, RunParams, codex_config, run_turn, start_thread
from .ask import fresh_reply
from .forms import DEFAULT_ANSWER_PATH, RUN_METHOD
from .ports import RUN_PORTS, TAG_ASK, TAG_DONE, TAG_EXCEPTION, TAG_LIMIT
from .settings import agent_env, config_overrides, parse_settings, resolve_workspace
from .vars import Resolver, read_scope

#: Values stashed on the job's context by the namer, for the handler.
CLIENT_KEY = "codex.client"
ERROR_KEY = "codex.namer.error"
RESUMED_KEY = "codex.resumed"

#: How long the namer may spend opening a thread. The runtime waits 15s for a
#: job id, so this must leave room to answer.
OPEN_TIMEOUT_SECONDS = 10

_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}
_WEB_SEARCH = {"disabled", "cached", "live"}

#: A job id this plugin minted is a Codex thread id. It must also be a usable
#: NATS subject token and at least 10 characters, which fractal-core enforces.
_THREAD_ID = re.compile(r"^[A-Za-z0-9_:-]{10,}$")


def is_thread_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_THREAD_ID.match(value))


def run_action(stops: jobstop.Registry) -> Action:
    return Action(
        method=RUN_METHOD,
        title="Run Codex",
        description=(
            "Give Codex a task to carry out on the plugin's host, in an "
            "OS-sandboxed thread that survives between runs. Routes to a human "
            "when Codex needs a decision."
        ),
        icon=Icon(icon="robot-outline"),
        tags={"class": "agent"},
        form=forms.run_form().build(),
        outbound=RUN_PORTS,
        # The namer comes FIRST: stops.middleware files the job under job.job_id
        # as of when it runs, and that id must already be the thread's.
        middleware=use(namer, stops.middleware),
        request_handler=handle_run,
    )


# ------------------------------------------------------------- the namer --



def overrides_for(conn, inp: dict[str, Any]) -> list[str]:
    """Every ``--config`` override this run implies.

    Web search and command network access are Codex *config* keys, not turn
    options, so they are decided here — where the node's form is — and handed to
    the client both the namer and the handler build.

    One rule is the API's, not ours: a turn at ``minimal`` effort is refused
    outright while the web-search tool is available ("The following tools cannot
    be used with reasoning.effort 'minimal': web_search"), so minimal effort
    turns it off unless the node asked for a specific mode.
    """
    out = list(config_overrides(conn))

    web = _one_of(inp.get("webSearch"), _WEB_SEARCH, "")
    if not web and str(inp.get("effort") or "").strip() == "minimal":
        web = "disabled"
    if web:
        out.append(f'web_search="{web}"')

    if inp.get("networkAccess") is not None:
        allowed = "true" if inp.get("networkAccess") else "false"
        out.append(f"sandbox_workspace_write.network_access={allowed}")
    return out


async def namer(ctx: JobContext, job: Job) -> JobContext:
    """Decide the job's id: the Codex thread this run speaks to.

    Reattaching costs nothing. Opening a new thread is one app-server call, with
    no model work in it, bounded by :data:`OPEN_TIMEOUT_SECONDS`. A failure here
    is *not* a rejection — the flow should see a failed node, not a request that
    never arrived — so it is stashed and reported by the handler.
    """
    body = cast_request_to(job.req.data)  # raises ⇒ the request is rejected
    inp = body.body if isinstance(body.body, dict) else {}
    registry = body.registry if isinstance(body.registry, dict) else {}

    previous = registry.get("jobId")
    wants_new = str(inp.get("session") or "continue").strip() == "new"
    if not wants_new and is_thread_id(previous) and previous != job.job_id:
        # The node's own last conversation. Reattach; start nothing.
        return with_job_id_context(ctx, previous).with_value(RESUMED_KEY, True)

    conn = parse_settings(inp.get("settings"))
    where = resolve_workspace(None, conn, os.environ.get("CODEX_WORKSPACE_ROOT") or os.getcwd())
    params = RunParams(
        prompt="",
        thread_id="",
        cwd=where.cwd or os.getcwd(),
        env=agent_env(conn),
        config_overrides=overrides_for(conn, inp),
        model=(inp.get("model") or "").strip() or conn.model,
        sandbox=_one_of(inp.get("sandbox"), _SANDBOXES, ""),
        codex_bin=(os.environ.get("CODEX_EXECUTABLE") or "").strip(),
        developer_instructions=str(inp.get("developerInstructions") or "").strip(),
    )

    try:
        client, _thread, thread_id = await asyncio.wait_for(start_thread(params), OPEN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return ctx.with_value(
            ERROR_KEY,
            f"codex did not open a thread within {OPEN_TIMEOUT_SECONDS}s on this host — "
            "check that the runtime is installed and that a credential is available",
        )
    except Exception as exc:  # noqa: BLE001
        return ctx.with_value(ERROR_KEY, f"codex could not start on this host: {exc}")

    if not is_thread_id(thread_id):
        await _shut(client)
        return ctx.with_value(
            ERROR_KEY,
            f"codex returned a thread id this plugin cannot use as a job id: {thread_id!r}",
        )

    named = with_job_id_context(ctx, thread_id).with_value(CLIENT_KEY, client)
    # Whatever happens next — including a later middleware rejecting — the
    # runtime process we just started must not be left behind.
    named.on_done(lambda _cause: asyncio.ensure_future(_shut(client)))
    return named


async def _shut(client: Any) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------ the handler --


async def handle_run(job: Job) -> None:
    ctx = job.context()

    failed = ctx.value(ERROR_KEY)
    if failed:
        await _fail(job, str(failed), {})
        return

    try:
        body = cast_request_to(job.req.data)
    except Exception as exc:  # noqa: BLE001
        await job.done_with_error(f"unreadable request: {exc}")
        return

    inp = body.body if isinstance(body.body, dict) else {}
    registry = body.registry if isinstance(body.registry, dict) else {}
    resuming = bool(ctx.value(RESUMED_KEY))
    client = ctx.value(CLIENT_KEY)
    thread_id = job.job_id

    previous_done_at = _number(registry.get("doneAt"))
    conn = parse_settings(inp.get("settings"))
    tokens = Resolver(job)

    # The workspace is a path an upstream node produced as often as one typed by
    # hand — the checkout a clone node just made, a directory per row of a list —
    # so it carries {{$.path}} tokens like every other free-text field, resolved
    # before the fence is checked.
    #
    # A node that NAMED a workspace and got nothing back is an error, not an
    # empty field: an empty workspace falls back to the connection's root, so
    # without this the agent would quietly work on the whole root tree instead of
    # the one directory the flow meant.
    wanted = str(inp.get("workspace") or "").strip()
    workspace = (await tokens.resolve(wanted)).strip() if wanted else ""
    if wanted != "" and (workspace == "" or workspace == wanted and "{{" in wanted):
        await _fail(
            job,
            f"workspace {wanted} resolved to nothing on this run's context — fix the path, "
            "or leave Workspace empty to use the connection's root deliberately",
            {"workspace": wanted},
        )
        return

    where = resolve_workspace(workspace, conn)
    if not where.ok:
        await _fail(job, where.error, {"workspace": workspace})
        return

    task = (await tokens.resolve(str(inp.get("prompt") or ""))).strip()
    if task == "":
        await _fail(job, "no task: write what Codex should do in the node's Task field", {})
        return

    # Extra writable directories are fenced by the same root as the workspace, or
    # the fence would leak through a field nobody thinks of as a path.
    extra: list[str] = []
    for row in await tokens.resolve_rows(inp.get("additionalDirectories")):
        resolved = resolve_workspace(row.strip(), conn)
        if not resolved.ok:
            await _fail(job, f"also-writable directory {row}: {resolved.error}", {"workspace": where.cwd})
            return
        extra.append(resolved.cwd)

    schema: dict | None = None
    if inp.get("structured") is True:
        parsed = _parse_schema(inp.get("outputSchema"))
        if isinstance(parsed, str):
            await _fail(job, parsed, {})
            return
        schema = parsed

    ask_human = inp.get("askHuman") is not False

    # A decision parked on the previous run is answered by whatever the flow left
    # at the answer path — the reply key of the Human in the Loop node wired to
    # the "Needs a human" port.
    answer = ""
    if resuming and ask_human:
        path = str(inp.get("answerFrom") or "").strip() or DEFAULT_ANSWER_PATH
        answer = fresh_reply(await read_scope(job, path), previous_done_at)

    prompt = f"The person you asked has answered:\n\n{answer}\n\n{task}" if answer else task

    params = RunParams(
        prompt=prompt,
        thread_id=thread_id,
        resuming=resuming,
        answer=answer,
        cwd=where.cwd,
        additional_directories=extra,
        env=agent_env(conn),
        config_overrides=overrides_for(conn, inp),
        model=str(inp.get("model") or "").strip() or conn.model,
        effort=_one_of(inp.get("effort"), _EFFORTS, ""),
        sandbox=_one_of(inp.get("sandbox"), _SANDBOXES, "workspace-write"),
        network_access=bool(inp.get("networkAccess")) if inp.get("networkAccess") is not None else None,
        web_search=_one_of(inp.get("webSearch"), _WEB_SEARCH, ""),
        skip_git_repo_check=bool(inp.get("skipGitRepoCheck")),
        output_schema=schema,
        ask_human=ask_human,
        max_tokens=_positive(inp.get("maxTokens")),
        max_seconds=_positive(inp.get("maxSeconds")),
        codex_bin=(os.environ.get("CODEX_EXECUTABLE") or "").strip(),
        developer_instructions=(await tokens.resolve(str(inp.get("developerInstructions") or ""))).strip(),
    )

    frames = _Progress(job)
    outcome = await run_turn(params, frames.step, codex=client)
    await frames.drain()

    # The runtime stops answering a job it has concluded, so a turn aborted with
    # its flow must not report. (jobstop cancelled the context; the stop is the
    # conclusion.)
    if ctx.canceled:
        return

    await commit(job, outcome, workspace=where.cwd, resumed=resuming, answered=answer != "")


# ------------------------------------------------------------- committing --


async def commit(job: Job, outcome: Outcome, *, workspace: str, resumed: bool, answered: bool) -> None:
    """One terminal call per path, with the branch named first.

    ``cmd_next_filter`` before the terminal command, in every case including
    failure: an error in inflow is information, not flow control, so a failed
    node still routes — down the branch the flow author wired to "Failed".
    """
    stats = outcome.stats
    base: dict[str, Any] = {
        "threadId": outcome.thread_id,
        "resumed": resumed or None,
        "workspace": workspace,
        "answeredQuestion": answered or None,
        "items": stats.items,
        "commands": stats.commands,
        "filesChanged": stats.files_changed,
        "tokens": {
            "input": stats.tokens_in,
            "output": stats.tokens_out,
            "total": stats.tokens_total,
            "contextWindow": stats.context_window,
        },
        "durationMs": stats.duration_ms,
        "model": stats.model or None,
        "observedAt": int(time.time()),
    }

    if outcome.kind == "done":
        await job.cmd_next_filter([TAG_DONE])
        data = {**base, "state": "done", "text": outcome.text}
        if outcome.output is not None:
            data["output"] = outcome.output
        await job.done(data)
        return

    if outcome.kind == "ask":
        # A successful job that says "not yet": the node did look, and the honest
        # answer is that a person has to speak. Failing here would route the
        # error branch for a thread that is perfectly healthy.
        await job.cmd_next_filter([TAG_ASK])
        await job.done(
            {
                **base,
                "state": "ask",
                "text": outcome.text,
                "question": outcome.question.to_data() if outcome.question else {},
            }
        )
        return

    if outcome.kind == "limit":
        await job.cmd_next_filter([TAG_LIMIT])
        await job.done({**base, "state": "limit", "text": outcome.text, "limit": {"reason": outcome.reason}})
        return

    await job.cmd_next_filter([TAG_EXCEPTION])
    # Partial state is still worth having: the thread id is how a human, or a
    # later run, picks the conversation back up.
    await job.done_with_error_data(
        outcome.message,
        {**base, "state": "error", "text": outcome.text, "error": outcome.message},
    )


async def _fail(job: Job, message: str, extra: dict[str, Any]) -> None:
    """Fail before the agent ever ran: nothing to report but the fix."""
    await job.cmd_next_filter([TAG_EXCEPTION])
    await job.done_with_error_data(message, {"state": "error", "error": message, **extra})


# ----------------------------------------------------------------- bits --


class _Progress:
    """Progress frames, serialized and throttled.

    Commands must be awaited (an un-awaited one is dropped) but the agent loop
    cannot wait on the canvas, so each frame becomes a task and they are drained
    before the job is concluded.
    """

    def __init__(self, job: Job, min_gap: float = 0.5) -> None:
        self._job = job
        self._gap = min_gap
        self._last = 0.0
        self._tasks: list[asyncio.Task] = []
        self._seen = 0

    def step(self, title: str, content: str, count: int) -> None:
        if not title:
            return
        now = time.monotonic()
        if now - self._last < self._gap:
            return
        self._last = now
        self._seen = max(self._seen, count)
        pct = min(90, 5 + self._seen * 3)
        self._tasks.append(asyncio.create_task(self._send(pct, title, content)))

    async def _send(self, pct: int, title: str, content: str) -> None:
        try:
            await self._job.progress(pct, Frame(title=title, content=content))
        except Exception:  # noqa: BLE001 — progress is advisory, never fatal
            pass

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()


def _one_of(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else fallback


def _positive(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_schema(text: Any) -> dict | str:
    raw = str(text or "").strip()
    if raw == "":
        return "structured answer is on but the answer schema is empty: write a JSON Schema, or turn it off"
    try:
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return f"the answer schema is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return 'the answer schema must be a JSON Schema object, e.g. {"type":"object","properties":{…}}'
    return parsed
