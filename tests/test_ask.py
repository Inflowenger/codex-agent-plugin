"""Question out, answer in — the two translations the human port rests on."""

from codex_agent.ask import (
    ACCEPT,
    DECLINE,
    approval_question,
    decision_of,
    fresh_reply,
    render_reply,
    structured_question,
)


def test_a_command_approval_reads_as_prose_a_person_can_answer():
    q = approval_question(
        "item/commandExecution/requestApproval",
        {"command": ["rm", "-rf", "build"], "cwd": "/srv/ws", "reason": "stale artefacts"},
    )
    assert q.kind == "approval"
    assert "rm -rf build" in q.prompt
    assert "stale artefacts" in q.prompt
    assert q.details["command"] == "rm -rf build"


def test_a_file_change_approval_lists_the_paths():
    q = approval_question("item/fileChange/requestApproval", {"changes": {"a.py": {}, "b.py": {}}})
    assert q.details["paths"] == ["a.py", "b.py"]
    assert "a.py" in q.prompt


def test_an_unreadable_request_still_produces_a_question():
    q = approval_question("item/commandExecution/requestApproval", None)
    assert q.prompt and q.details == {}


def test_a_reply_is_read_as_a_decision():
    assert decision_of("yes, go ahead") == ACCEPT
    assert decision_of("Approve") == ACCEPT
    assert decision_of("no — decline that") == DECLINE
    assert decision_of("deny") == DECLINE


def test_free_text_is_not_mistaken_for_a_decision():
    # It must park/refuse rather than invent consent.
    assert decision_of("use pytest instead of unittest") is None
    assert decision_of("") is None


def test_the_first_word_wins_when_a_reply_says_both():
    assert decision_of("no, do not approve this") == DECLINE


def test_approve_inside_a_longer_word_is_not_a_decision():
    assert decision_of("disapproved-by-policy") is None


def test_a_structured_answer_can_ask_for_a_person():
    q = structured_question({"status": "needs_human", "question": {"question": "Which DB?", "options": ["pg"]}})
    assert q is not None and "Which DB?" in q.prompt and q.details["options"] == ["pg"]


def test_an_ordinary_structured_answer_asks_nobody():
    assert structured_question({"status": "ok", "summary": "done"}) is None
    assert structured_question("not json") is None


def test_a_human_in_the_loop_outcome_renders_to_its_answers():
    assert render_reply({"answers": [{"question": "Which?", "answer": "pg"}]}) == "Which? — pg"
    assert render_reply("just words") == "just words"
    assert render_reply({"text": "from text"}) == "from text"


def test_a_reply_older_than_this_nodes_last_run_is_not_reused():
    stale = {"text": "yes", "closedAt": 100}
    assert fresh_reply(stale, 200) == ""      # settled before this node last finished
    assert fresh_reply(stale, 50) == "yes"    # settled after it: meant for this question
    assert fresh_reply({"text": "yes"}, 200) == "yes"  # unprovable: use it
