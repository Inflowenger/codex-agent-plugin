"""The node's outbound branches.

These are the plugin's half of a contract with the canvas: the whole list is
served on the action, the designer renders one output port per entry — labelled
by ``title``, explained by ``description`` — and stamps that port's ``tags`` onto
every edge drawn from it. At run time the handler names the branch(es) that fire
with ``job.cmd_next_filter(tags)``.

Two consequences, because they are what makes ports a contract rather than
configuration:

* They are not editable on the canvas. A flow author can leave a port unwired,
  but cannot remove or rename one — the shape of the branch belongs to the plugin.
* An action that declares ports has NO default handle, so every edge leaving it
  must name one. A port left unwired simply ends that branch of the run.

Tag names are the wire contract: renaming one orphans the edges already drawn
from its port, so treat them as fixed once published.

The set matches the Claude Agent node deliberately — a flow author who knows one
node knows the other — even though what *produces* an ``ask`` differs (Codex
raises an approval request or a question; Claude defers a tool call).
"""

from inflow_plugin_sdk import OutboundPort

#: The turn finished and Codex has an answer.
TAG_DONE = "done"
#: Codex needs a person — an approval it cannot grant itself, or a question.
TAG_ASK = "ask"
#: The turn hit a ceiling this plugin enforces (tokens, time) with work left.
TAG_LIMIT = "limit"
#: The reserved failure tag. The runtime fires it like any other, so a failed
#: node still routes — the flow decides what a failure means.
TAG_EXCEPTION = "_exception"

RUN_PORTS = [
    OutboundPort(
        title="Answer",
        tags=[TAG_DONE],
        description=(
            "Codex finished this turn. Its answer — and the structured output, "
            "when a schema was set — is on the node's scope."
        ),
    ),
    OutboundPort(
        title="Needs a human",
        tags=[TAG_ASK],
        description=(
            "Codex asked for a decision: an approval outside its sandbox, or a "
            "question it cannot settle. Wire this to a Human in the Loop node "
            "(park mode) and bring the reply back to this node — the same thread "
            "resumes with the answer."
        ),
    ),
    OutboundPort(
        title="Limit reached",
        tags=[TAG_LIMIT],
        description=(
            "The turn ran past the token or time budget set on the node and was "
            "interrupted with work unfinished. Run the node again to continue the "
            "same thread, or branch to an escalation."
        ),
    ),
    OutboundPort(
        title="Failed",
        tags=[TAG_EXCEPTION],
        description="Codex could not run, or the turn ended in an error. The reason is on the node's output.",
    ),
]
