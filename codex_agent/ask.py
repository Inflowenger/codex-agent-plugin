"""Ask-human: how a decision Codex needs mid-run becomes a branch on the canvas.

A flow run is seconds long and a person is not. The runtime gives up on a job
that sends no command for its idle window, so a plugin that blocked waiting for
an answer would hang the node and lose the thread.

Codex raises two kinds of decision the flow did not give it:

1. **An approval request.** The app-server sends the client a JSON-RPC *request*
   — ``item/commandExecution/requestApproval`` or ``item/fileChange/requestApproval``
   — when the agent wants to step outside its sandbox. The Python SDK answers it
   through a caller-supplied ``approval_handler``; left alone, its default accepts
   everything, which is precisely what a flow must not do silently.
2. **A question.** Codex has no ``AskUserQuestion`` tool, so an open question is
   raised the one way a turn can end on purpose: the agent reports
   ``{"status": "needs_human", "question": …}`` under the node's answer schema.

Either way the turn ends, the node fires its "Needs a human" port, **nothing is
held open**, and the person's reply arrives on a later run — which resumes the
same thread. This module owns the translation in both directions: a decision
Codex wants → prose a person reads, and a person's words → the answer Codex gets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: The JSON-RPC methods the app-server uses to ask the client for an approval.
APPROVAL_METHODS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    # Legacy names, still sent by older runtimes.
    "execCommandApproval",
    "applyPatchApproval",
)

#: The decision values this plugin sends back. ``ACCEPT`` is the value the SDK's
#: own default handler uses, so it is known-good; ``DECLINE`` is the refusal.
ACCEPT = "accept"
DECLINE = "decline"


@dataclass
class Question:
    """A decision Codex is waiting on, as committed to the flow's context."""

    #: ``approval`` (Codex wants to do something) or ``question`` (Codex asked).
    kind: str
    #: What is being asked, rendered as prose ready to drop into a Human in the
    #: Loop node's prompt. A person should never have to walk a JSON shape.
    prompt: str
    #: The JSON-RPC method, for an approval. Empty for a question.
    method: str = ""
    #: What Codex wants to do, in fields a flow can route on rather than read.
    details: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, Any]:
        return {"kind": self.kind, "prompt": self.prompt, "method": self.method, "details": self.details}


def approval_question(method: str, params: dict | None) -> Question:
    """Turn an approval request into something a person can answer.

    The params are read defensively: the app-server's request shapes are not in
    the SDK's generated models, they differ between runtime versions, and a
    missing field must degrade into a vaguer question rather than a crash.
    """
    p = params or {}
    details: dict[str, Any] = {}
    for key in ("command", "cwd", "reason", "risk", "call_id", "item_id", "thread_id", "turn_id"):
        value = p.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip() != "":
            details[key] = value
    if isinstance(p.get("command"), list):
        details["command"] = " ".join(str(c) for c in p["command"])

    changes = p.get("changes") or p.get("file_changes")
    if isinstance(changes, dict):
        details["paths"] = sorted(str(k) for k in changes)
    elif isinstance(changes, list):
        details["paths"] = [str(c.get("path", c)) if isinstance(c, dict) else str(c) for c in changes]

    return Question(kind="approval", method=method, prompt=_render_approval(method, details), details=details)


def _render_approval(method: str, details: dict[str, Any]) -> str:
    what = "run a command" if "ommand" in method.lower() or "exec" in method.lower() else "change files"
    lines = [f"**Codex is asking permission to {what}.** It is outside what its sandbox allows on its own."]

    if details.get("command"):
        lines.append(f"\nCommand:\n```\n{details['command']}\n```")
    if details.get("paths"):
        shown = details["paths"][:12]
        more = len(details["paths"]) - len(shown)
        listing = "\n".join(f"- {p}" for p in shown)
        lines.append(f"\nFiles:\n{listing}" + (f"\n- …and {more} more" if more > 0 else ""))
    if details.get("cwd"):
        lines.append(f"\nWorking directory: `{details['cwd']}`")
    if details.get("reason"):
        lines.append(f"\nWhy Codex says it needs this: {details['reason']}")

    lines.append(
        "\nReply **approve** to let it go ahead, or **decline** to refuse. Anything "
        "else you write is passed to Codex as instructions, and it continues the "
        "same thread with your words in hand."
    )
    return "\n".join(lines)


def structured_question(payload: Any) -> Question | None:
    """Read a ``needs_human`` answer out of a structured turn result.

    Returns ``None`` when the turn did not ask for anybody — which is the normal
    case and must stay cheap.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status", "")).strip().lower()
    if status not in ("needs_human", "needs-human", "ask", "blocked"):
        return None

    raw = payload.get("question")
    if isinstance(raw, dict):
        text = str(raw.get("question") or raw.get("text") or raw.get("prompt") or "").strip()
        options = [str(o) for o in raw.get("options", []) if str(o).strip() != ""]
    else:
        text = str(raw or payload.get("text") or "").strip()
        options = []

    if text == "":
        text = "Codex stopped and asked for a person, but did not say what it needs."

    body = [f"**{text}**"]
    if options:
        body.append("\n" + "\n".join(f"- {o}" for o in options))
    body.append(
        "\nAnswer in your own words, or name one of the options above. Your reply "
        "goes straight back to Codex, which continues the same thread."
    )
    return Question(kind="question", prompt="\n".join(body), details={"question": text, "options": options})


_APPROVE_WORDS = ("approve", "approved", "accept", "accepted", "allow", "yes", "go ahead", "ok", "okay", "proceed")
_DECLINE_WORDS = ("decline", "declined", "deny", "denied", "reject", "rejected", "no", "stop", "don't", "do not")


def decision_of(reply: str) -> str | None:
    """Read a person's words as an approval decision.

    Returns :data:`ACCEPT`, :data:`DECLINE`, or ``None`` when the reply is
    neither — which is not a failure: free text is passed to Codex as
    instructions instead, and the request is refused so it cannot proceed on a
    decision nobody made.
    """
    text = (reply or "").strip().lower()
    if text == "":
        return None
    declined = _first_hit(text, _DECLINE_WORDS)
    approved = _first_hit(text, _APPROVE_WORDS)
    if declined is not None and (approved is None or declined < approved):
        return DECLINE
    if approved is not None:
        return ACCEPT
    return None


def _first_hit(text: str, words: tuple[str, ...]) -> int | None:
    best: int | None = None
    for w in words:
        for m in re.finditer(re.escape(w), text):
            i = m.start()
            before = " " if i == 0 else text[i - 1]
            after = " " if i + len(w) >= len(text) else text[i + len(w)]
            if re.match(r"[^a-z0-9]", before) and re.match(r"[^a-z0-9]", after):
                if best is None or i < best:
                    best = i
    return best


def fresh_reply(value: Any, previous_run_done_at: float) -> str:
    """The reply, but only if it can have been meant for this node's question.

    The answer path is an ordinary place in the context: it still holds the LAST
    reply after the loop moves on, so a later run — one that hit its budget, say,
    and is simply being run again — would otherwise find an old answer sitting
    there and hand it to Codex as though a person had just spoken.

    A Human in the Loop outcome is stamped with ``closedAt``, and the engine tells
    the node when its own previous run finished (``_registry.doneAt``). A reply
    settled BEFORE this node last finished belongs to an earlier round. When
    either timestamp is missing nothing can be proven, and the reply is used:
    answering a waiting question is the common case, and the alternative is a
    conversation that silently never hears back.
    """
    closed_at = _closed_at_of(value)
    if closed_at > 0 and previous_run_done_at > 0 and closed_at < previous_run_done_at:
        return ""
    return render_reply(value)


def _closed_at_of(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    for key in ("closedAt", "answeredAt", "updatedAt"):
        v = value.get(key)
        try:
            n = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 0.0


def render_reply(value: Any) -> str:
    """Render whatever the flow left at the answer path into words to hand back.

    The Human in the Loop node commits an object — ``{status, answers:
    [{question, answer}], messages, text, closedAt}`` — but the path may equally
    point at a plain string another node wrote, or at a whole sub-document.
    Accept all three rather than making the flow author reshape their context to
    suit this plugin.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(x for x in (render_reply(v) for v in value) if x)
    if not isinstance(value, dict):
        return str(value)

    answers = value.get("answers")
    if isinstance(answers, list):
        lines = []
        for a in answers:
            if not isinstance(a, dict):
                continue
            q = str(a.get("question", "")).strip()
            ans = str(a.get("answer", "")).strip()
            if ans:
                lines.append(f"{q} — {ans}" if q else ans)
        if lines:
            return "\n".join(lines)
    for key in ("text", "answer", "reply", "message", "value", "result"):
        v = value.get(key)
        if isinstance(v, str) and v.strip() != "":
            return v.strip()
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
