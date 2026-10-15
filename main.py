"""codex-agent — an Inflowenger plugin node that runs OpenAI Codex as an agent on
the plugin's own host.

It is unusual among inflow plugins in three ways, and all three are the point:

1. **It holds a CONVERSATION, not a request.** Each run of the node is one turn
   of a durable Codex thread. Codex mints the thread id, so the node takes that
   id as its own job id — which makes the engine's memory of the node's last run
   (``_registry.jobId``) the handle on the conversation. No database, no
   in-memory map, nothing to lose on restart.

2. **It can ASK.** When Codex wants to step outside its sandbox, or cannot settle
   a question the flow did not answer, the turn ends and the node fires its
   "Needs a human" port. Wire that port to a Human in the Loop node and bring the
   reply back: the same thread resumes with the answer.

3. **The fence is real.** Codex sandboxes the agent at the operating system
   (Seatbelt on macOS, bubblewrap on Linux), so the workspace is a boundary the
   model cannot talk its way around — not merely a working directory.

What it does NOT do: hold a credential. A settings profile may carry an API key,
and when it does not, the agent runs as whatever the host's ``codex login`` is
already authenticated as. Either way the key never reaches a form, a log line or
the node's output.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from inflow_plugin_sdk import (
    PluginIntro,
    chain_signals,
    jobstop,
    log_signals,
    new_plugin,
    with_dot_env,
    with_timeout,
)

from codex_agent import __version__
from codex_agent.actions import run_action
from codex_agent.forms import settings_form
from codex_agent.metas import metas, probe
from codex_agent.settings import parse_settings, resolve_workspace

# An agent turn is slow by nature: it reads files, runs commands and thinks. This
# deadline covers the plugin's own commands to the runtime (progress, context
# reads, done), not the turn — but a tight default would make a busy host look
# like a broken one.
SEND_TIMEOUT_SECONDS = 60


def load_manual() -> str:
    """The plugin's page in Extensions. Authored as Markdown next to this module
    so the prose lives in a real document; the host turns each fenced
    ```inflow-meta block into a Run button for that meta method."""
    try:
        return (Path(__file__).parent / "MANUAL.md").read_text(encoding="utf-8")
    except OSError as exc:
        print(f"codex-agent: MANUAL.md not found, serving an empty manual: {exc}")
        return ""


async def main() -> None:
    env_file = os.environ.get("INFLOW_ENV_FILE") or ".env.inflow"
    # The dotenv carries the platform identity only: PLUGIN_ID, INFRA_CRED,
    # INFRA_URL. No OpenAI configuration ever belongs in it.
    p = await new_plugin(with_dot_env(env_file), with_timeout(SEND_TIMEOUT_SECONDS))

    # One stop registry for the whole plugin: a turn aborted with its flow stops
    # its runtime instead of carrying on writing to someone's disk.
    stops = jobstop.Registry()
    p.on_signal(chain_signals(log_signals("codex-agent"), stops.on_signal))

    settings = settings_form().build()
    p.intro(
        PluginIntro(
            name="CODEX AGENT",
            author="FloMorphic",
            version=__version__,
            settings=settings,
            manual=load_manual(),
        )
    )
    p.required_params(settings)

    action = run_action(stops)
    p.add_action(action)
    p.add_meta(*metas())

    await p.start()

    ports = ", ".join("|".join(o.tags) for o in (action.outbound or []))
    print(f"codex-agent plugin {__version__} ready: action {action.method}, ports {ports}")
    await report_host()

    # start() only wires subscriptions; the process has to stay alive to serve them.
    await asyncio.Event().wait()


async def report_host() -> None:
    """Say on startup what the agent will actually be — runtime version and which
    credential — so a misconfigured host is obvious in the logs rather than on the
    first flow run. Best effort: a host without a usable Codex still serves the
    node's forms, and the node's own error will name the fix."""
    conn = parse_settings({})
    where = resolve_workspace(None, conn, os.environ.get("CODEX_WORKSPACE_ROOT") or os.getcwd())
    if not where.ok:
        print(f"codex-agent: no default workspace on this host: {where.error}")
        return
    try:
        info = await probe(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"codex-agent: Codex did not start on this host — {exc}")
        print("codex-agent: nodes will fail until it is installed and logged in, or a profile carries an API key")
        return
    print(
        f"codex-agent: Codex {info.get('version', '?')} on this host, "
        f"credential {info.get('credential', '?')}, workspace root {where.cwd}"
    )


if __name__ == "__main__":
    asyncio.run(main())
