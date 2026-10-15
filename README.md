# Codex Agent — `codex-agent`

An Inflowenger **plugin node** that runs **OpenAI Codex as an agent on the
plugin's own host**, built on [`inflowenger-plugin-sdk`](https://pypi.org/project/inflowenger-plugin-sdk/)
and [`openai-codex`](https://pypi.org/project/openai-codex/) (the Codex **Python**
SDK, which drives the Codex app-server over JSON-RPC).

It is the sibling of [`claude-agent`](../claude-agent) — same node shape, same
four ports, same human loop — over a different agent. Where the two differ, it is
because the agents differ, and those differences are the interesting part of this
README.

## Why Python, when `claude-agent` is Node

Because OpenAI ships two Codex SDKs and they are **not** equivalent:

| | `@openai/codex-sdk` (TS) | `openai-codex` (Python) |
|---|---|---|
| Transport | `codex exec --experimental-json`, a one-way JSONL pipe | the **app-server**, bidirectional JSON-RPC |
| Thread id before a turn runs | ✗ (null until turn 1) | ✓ `thread_start()` |
| Approval requests reach the caller | ✗ | ✓ `approval_handler` |
| `thread_fork` / `thread_list` / `models()` / `account()` | ✗ | ✓ |
| `steer()` / `interrupt()` mid-turn | ✗ | ✓ |

The TS SDK is the more popular one by a wide margin, and it is the one labelled
stable — but every mechanism this node is built on is missing from it. So this
plugin is Python, which the Inflowenger SDK supports as a first-class language.

## What makes it different from every other plugin here

1. **It holds a conversation, not a request.** Each run of the node is one turn
   of a durable Codex thread.

2. **The thread names the job.** Codex mints its own thread id and will not take
   one from us — the mirror image of `claude-agent`, which pins the session id to
   the job id. So the node does the opposite and **adopts the thread's id as the
   inflow job id**, in a namer middleware that runs before the job is accepted.
   `thread_start()` is an app-server call that mints the id with no model work in
   it: **measured at 0.85s on a cold host**, well inside the runtime's 15s budget.
   From then on the engine's own record of the node's last run
   (`_registry.jobId`) *is* the handle on the conversation — no database, no
   in-memory map, nothing to lose when the plugin restarts.

3. **It can stop and ask.** When Codex wants to step outside its sandbox, the
   app-server sends the *client* a JSON-RPC request. The node answers it, ends
   the turn, and fires its **Needs a human** port.

4. **The fence is real.** `read-only` and `workspace-write` are enforced by the
   OS. `claude-agent`'s README has to admit that its workspace root fences the
   node but not the agent; this one does not.

## The human loop

```
[ Run Codex ] --Answer------------------> … the rest of the flow
      |      \--Limit reached-----------> (run again, or escalate)
      |      \--Failed------------------> (handle it)
      |
      '---------Needs a human----> [ Human in the Loop ]  (park, key: humanReply)
                                            |
                                            '--> back into [ Run Codex ]
```

| | |
|---|---|
| **run 1** | The namer opens a thread; its id `A` becomes the job id. Codex asks to run a command outside its sandbox. Our `approval_handler` records the request, **refuses** it (so nothing proceeds on a decision nobody made), and the turn is interrupted. The node commits the question and fires `ask`. |
| **between** | The flow parks at a Human in the Loop node. **Nothing is held open** — no process, no connection, no node sitting at 40%. The thread waits in `~/.codex/sessions`. |
| **run 2** | The person's reply is at `$.humanReply`. The engine hands the node `_registry.jobId = A`, the namer reattaches to thread `A`, and the reply is read as a decision: **approve** accepts the next approval the turn raises, **decline** refuses it, and anything else is passed to Codex as instructions. Codex carries on with its context intact. |

**Why not simply wait inside the handler?** Because a flow run is seconds long
and a person is not: the runtime abandons a job that sends no command for its
idle window, and the turn would die with the thread. Ending the turn and
resuming later is what makes the node's two halves two runs of one node.

### How this differs from `claude-agent`'s loop

Claude Code has a `defer` decision built for exactly this: the tool call stays
pending in the transcript and re-fires on resume. **Codex has no equivalent** —
its `PreToolUse` hooks speak almost the same language but offer only
`allow`/`deny` (`ask` is parsed and treated as a hook *failure*), and hooks are
external processes rather than in-process callbacks. So this node parks the
*question* rather than the *call*: the request is refused, the turn ends, and
the answer arrives as instructions on the next turn. The conversation survives;
the individual tool call does not.

## The ports

| Port | Tag | Fires when |
|---|---|---|
| Answer | `done` | The turn finished. `text`, and `output` when a schema was set. |
| Needs a human | `ask` | Codex needs a decision. A **successful** job — a parked question is not a failure. |
| Limit reached | `limit` | The node's own token or time budget cut the turn short. Run it again to continue. |
| Failed | `_exception` | Codex could not run, or the turn ended in error. Routes *and* reports. |

## Budgets are ours, not Codex's

Codex enforces **no** turn cap and **no** spend cap, and reports tokens rather
than dollars. So the node does it: **Token budget** and **Time budget** are
watched as the turn streams, and breaching one interrupts the turn and fires
`limit`. Leave them at 0 for no ceiling.

## What a run commits

| Field | When | What |
|---|---|---|
| `state` | always | `done` · `ask` · `limit` · `error` |
| `threadId` | always | the thread this turn ran as — also the job id |
| `resumed` | continuing | this turn continued an existing thread |
| `text` | always | Codex's own answer for the turn |
| `output` | with a schema | the structured answer, parsed |
| `question` | `ask` | `{kind, prompt, method, details}`; `prompt` is prose, ready for a person |
| `limit.reason` | `limit` | `token budget` · `time budget` · `interrupted` |
| `answeredQuestion` | after an answer | the turn delivered a person's reply |
| `items` · `commands` · `filesChanged` · `tokens` · `durationMs` · `workspace` · `observedAt` | always | what the turn did and spent |

## The form

Free-text fields take `{{$.path}}` tokens, resolved from the run's own context
before the prompt is sent (`$this` works too, for a node scoped over a list).

- **Task** — what Codex should do.
- **Where** — *Workspace* (must sit inside the connection's root), *Also
  writable* (with a **List directories** button), *Allow a non-git workspace*.
  Codex refuses to run outside a git repository unless told to.
- **Model** — *Model* (with a live **List models** button), *Effort*.
- **What it may do** — *Sandbox*, *Allow network access*, *Web search*. There is
  no tool picker, because Codex has no per-tool allow-list: its fence is the
  sandbox, which is a stronger one.
- **How it ends** — *Thread* (`continue` resumes the node's own last
  conversation, which is what makes the human loop work), *Token budget*,
  *Time budget*.
- **Structured answer** — *Return structured data*, *Answer schema*.
- **Human in the loop** — *Let Codex ask a person* (on), *Answer path*
  (`$.humanReply`), *Extra instructions*.

## The connection

| Field | Meaning |
|---|---|
| `apiKey` | Optional. Empty means the agent runs as the host's own `codex login`. |
| `baseUrl` | Optional. A gateway or proxy instead of the public API. |
| `model` | Default model for nodes that name none. |
| `workspaceRoot` | Where nodes may be pointed: a node outside this tree fails before Codex starts. Empty means unrestricted. |

Keys are matched leniently (case, spaces, dashes, underscores, and the obvious
synonyms), because profiles are typed by hand.

## Meta functions

All of these answer from the Codex app-server **without sending a token to a
model**, so they are free to run.

| Method | Purpose |
|---|---|
| `codex.meta.ping` | Connection test — runtime version, which credential is in use, workspace root. Also the settings form's `submitTo`, so saving a profile tests it. |
| `codex.meta.models` | The models *this host* offers; re-renders the open form with the model field as a drop-down. |
| `codex.meta.dirs` | The directories under the connection's workspace root, as rows for *Also writable*. |
| `codex.meta.threads` | The Codex threads already on this host. |

## Run

```bash
cp .env.inflow.example .env.inflow     # PLUGIN_ID / INFRA_CRED / INFRA_URL
pip install -r requirements.txt
python main.py
```

The plugin must already be provisioned in a space — that is where `PLUGIN_ID`,
`INFRA_CRED` and `INFRA_URL` come from. On startup it logs every subject it
subscribed and then what the host's Codex actually is — version and credential —
so a misconfigured host is obvious in the log rather than on the first flow run.

**Requirements on the host:** Python 3.11+, and Codex reachable (the bundled
runtime from `openai-codex` serves, or point `CODEX_EXECUTABLE` at another
build) and either logged in or given an API key. On Linux the sandbox wants
`bubblewrap` (`bwrap`) on `PATH`. `CODEX_WORKSPACE_ROOT` is the default fence
when no profile sets one.

## Safety

- **A resolved-to-nothing workspace fails.** An empty Workspace means the
  connection's root, so a `{{$.path}}` token the context cannot answer must not
  quietly become "the whole root tree" — the node fails and names the template.
- **"Also writable" grants write, not read.** Codex's `--add-dir` widens the
  sandbox. Those rows are fenced by the same workspace root as the workspace
  itself, or the fence would leak through a field nobody thinks of as a path.
- **`danger-full-access` removes the sandbox.** Use it only in a workspace you
  are willing to lose. `read-only` explores without changing anything.
- **The approval handler fails closed.** Without it, the SDK's default answers
  *accept* to every approval request. If a future `openai-codex` moves it, the
  node refuses to run anything but `read-only` rather than silently
  auto-approving. One test and one guard cover this.
- **A turn stops with its flow.** The action carries `jobstop`, so a stopped run
  ends the turn instead of leaving an agent writing to someone's disk. The
  conversation is not lost — the thread is on disk and the next run resumes it.
- **No credential reaches a form, a log line or the node's output.** A test
  walks the run form and asserts it.

## Develop

```bash
python -m pytest tests -q          # 47 tests, no platform, no spend
python main.py                     # run against a platform
```

| Path | What |
|---|---|
| [`main.py`](main.py) | wiring: intro, settings, action, metas, signals, start |
| [`codex_agent/actions.py`](codex_agent/actions.py) | the action: the namer that adopts the thread id, the answer path, the port contract |
| [`codex_agent/agent.py`](codex_agent/agent.py) | the only module that knows the Codex SDK |
| [`codex_agent/ask.py`](codex_agent/ask.py) | approval → prose, reply → decision |
| [`codex_agent/ports.py`](codex_agent/ports.py) | the outbound branches and their tags |
| [`codex_agent/forms.py`](codex_agent/forms.py) | both forms, generated from one declaration each |
| [`codex_agent/metas.py`](codex_agent/metas.py) | the free, token-less helpers |
| [`codex_agent/settings.py`](codex_agent/settings.py) | profile parsing, the workspace fence, subprocess env |
| [`codex_agent/vars.py`](codex_agent/vars.py) | `{{$.path}}` resolution |

## Status

Verified live against Codex 0.162.1 on a real host: runtime boot, the zero-token
probe (version + credential), thread minting (0.85s, a UUIDv7 usable as a job
id), client configuration and config overrides, the notification stream, and
error mapping. **The model call itself is unverified** — the host's API key
reports `Quota exceeded` — so the full turn, the approval park and the resume
need one run against a funded credential before this is trusted in a flow.
