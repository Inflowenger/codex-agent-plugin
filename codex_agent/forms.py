"""Both forms, generated from one declaration each.

A malformed form raises at start-up (``build()`` calls ``validate()``), where it
is a programming error rather than a dialog that will not open.

The run form is organised the way a person thinks about handing work to an
agent: *what to do*, *where*, *which model*, *what it may do*, *how it ends*,
*what comes back*, *who to ask*. Free-text fields take ``{{$.path}}`` tokens,
resolved from the run's own context before the prompt is sent (``$this`` works
too, for a node scoped over a list).

The section that differs most from the Claude Agent node is **What it may do**.
Claude Code is fenced by naming tools; Codex is fenced by the operating system.
So there is no tool picker here — there is a sandbox, an approval route and a
network switch, which is both a smaller form and a stronger fence.
"""

from __future__ import annotations

from inflow_plugin_sdk import formkit
from inflow_plugin_sdk.formkit import Option

#: Where a person's reply is read from by default — the Human in the Loop node's
#: reply key.
DEFAULT_ANSWER_PATH = "$.humanReply"

META_PING = "codex.meta.ping"
META_MODELS = "codex.meta.models"
META_DIRS = "codex.meta.dirs"
META_THREADS = "codex.meta.threads"

RUN_METHOD = "codex.agent.run"


def run_form() -> formkit.Form:
    return (
        formkit.form("Run Codex")
        .describe(
            "Give Codex a task to carry out on the plugin's host, in a thread that "
            "survives between runs. Every free-text field below takes {{$.path}} "
            "tokens from the flow's context."
        )
        .add(
            # ------------------------------------------------------ the task --
            formkit.text_area("prompt", "Task")
            .required()
            .describe(
                "What Codex should do. Takes {{$.path}} tokens — e.g. "
                "'Review {{$.clone.path}} and fix the failing tests'."
            ),
            # ----------------------------------------------------------- where --
            formkit.text("workspace", "Workspace").describe(
                "The directory Codex works in. Must sit inside the connection's "
                "workspace root. Takes {{$.path}} tokens, so it can be what an "
                "upstream node produced. Empty uses the root itself."
            ),
            formkit.list_("additionalDirectories", "Also writable")
            .describe(
                "Extra directories the sandbox may write in, besides the workspace. "
                "Press ↻ to list what is under the root. Note these grant WRITE "
                "access, not just read."
            )
            .lookup(META_DIRS, "List directories"),
            formkit.boolean("skipGitRepoCheck", "Allow a non-git workspace")
            .default(False)
            .describe(
                "Codex refuses to run outside a git repository, because without one "
                "it cannot show or undo what it changed. Tick this only for a "
                "workspace you are willing to lose."
            ),
            # ----------------------------------------------------------- model --
            formkit.text("model", "Model")
            .describe("Empty uses the profile's default. Press ↻ to list what this host offers.")
            .lookup(META_MODELS, "List models")
            .picks(RUN_METHOD),
            formkit.enum_("effort", "Effort", "", "low", "medium", "high", "xhigh", "max")
            .describe(
                "How hard the model thinks before acting. Empty uses the model's "
                "default. Not every model accepts every level — one that does not "
                "fails the turn and says so."
            ),
            # ---------------------------------------------------- what it may do --
            formkit.choice(
                "sandbox",
                "Sandbox",
                Option("workspace-write", "Write inside the workspace (default)"),
                Option("read-only", "Read only — explore, change nothing"),
                Option("danger-full-access", "Full access — no sandbox at all"),
            )
            .default("workspace-write")
            .describe(
                "Enforced by the operating system, not by the model: Seatbelt on "
                "macOS, bubblewrap on Linux. 'Full access' removes the fence "
                "entirely — use it only in a workspace you are willing to lose."
            ),
            formkit.boolean("networkAccess", "Allow network access")
            .default(False)
            .describe("Whether commands Codex runs may reach the network. Off is the safe default."),
            formkit.choice(
                "webSearch",
                "Web search",
                Option("", "Leave as the host configured it"),
                Option("disabled", "Off"),
                Option("cached", "Cached results only"),
                Option("live", "Live search"),
            ).describe("Codex's own search tool, which is separate from command network access."),
            # ------------------------------------------------------ how it ends --
            formkit.choice(
                "session",
                "Thread",
                Option("continue", "Continue this node's own conversation"),
                Option("new", "Start a fresh conversation every run"),
            )
            .default("continue")
            .describe(
                "'Continue' resumes the thread this node last ran — which is what "
                "makes the human loop work, and what lets a second run pick up where "
                "a budget cut the first one short."
            ),
            formkit.integer("maxTokens", "Token budget")
            .min(0)
            .describe(
                "Interrupt the turn once it has spent this many tokens, and fire "
                "'Limit reached'. 0 means no ceiling. Codex enforces no budget of "
                "its own, so this node does it."
            ),
            formkit.integer("maxSeconds", "Time budget (seconds)")
            .min(0)
            .describe("Interrupt the turn after this long and fire 'Limit reached'. 0 means no ceiling."),
            # --------------------------------------------------- what comes back --
            formkit.boolean("structured", "Return structured data")
            .default(False)
            .describe("Make Codex's final answer conform to a JSON Schema, committed as `output`."),
            formkit.text_area("outputSchema", "Answer schema")
            .describe('A JSON Schema object, e.g. {"type":"object","properties":{…}}.')
            .show_when("structured", True),
            # ------------------------------------------------------- the human --
            formkit.boolean("askHuman", "Let Codex ask a person")
            .default(True)
            .describe(
                "When Codex needs an approval or a decision the flow did not give "
                "it, end the turn and fire 'Needs a human' instead of refusing. Off "
                "means every such request is refused and Codex works around it."
            ),
            formkit.text("answerFrom", "Answer path")
            .default(DEFAULT_ANSWER_PATH)
            .describe(
                "Where this node reads the person's reply on the next run — the "
                "reply key of the Human in the Loop node wired to 'Needs a human'."
            )
            .show_when("askHuman", True),
            formkit.text_area("developerInstructions", "Extra instructions").describe(
                "House rules prepended to every turn of this node — coding standards, "
                "what never to touch, when to stop and ask."
            ),
        )
    )


def settings_form() -> formkit.Form:
    """The connection form.

    A settings profile here is as much *policy* as credential: the workspace root
    is the one directory tree any node of this plugin may be pointed at.
    """
    return (
        formkit.form("Codex host")
        .describe(
            "How this plugin's host runs Codex. Leave the API key empty to run as "
            "whatever the host's own `codex login` is authenticated as. Saving the "
            "profile tests it, and costs nothing."
        )
        .submit_to(META_PING)
        .add(
            formkit.secret("apiKey", "OpenAI API key").describe(
                "Optional. Empty means the agent runs as the host's own Codex login."
            ),
            formkit.text("baseUrl", "Base URL").describe(
                "Optional. A gateway or proxy instead of the public API."
            ),
            formkit.text("model", "Default model")
            .describe("Used by every node of this profile that names no model of its own.")
            .lookup(META_MODELS, "List models")
            .picks("codex.settings"),
            formkit.text("workspaceRoot", "Workspace root").describe(
                "The one directory tree nodes may be pointed at: a node outside it "
                "fails before Codex starts. Empty means unrestricted."
            ),
            formkit.text("test", "Test this host")
            .describe("Press ↻ to check the runtime, its credential and the default model.")
            .lookup(META_PING, "Test host"),
        )
    )
