"""The port contract. Renaming a tag orphans edges already drawn, so pin them."""

from codex_agent.ports import RUN_PORTS, TAG_ASK, TAG_DONE, TAG_EXCEPTION, TAG_LIMIT


def test_the_tags_are_the_wire_contract():
    assert [t for p in RUN_PORTS for t in p.tags] == [TAG_DONE, TAG_ASK, TAG_LIMIT, TAG_EXCEPTION]


def test_every_port_explains_itself_on_the_canvas():
    assert all(p.title and p.description for p in RUN_PORTS)


def test_failure_uses_the_runtimes_reserved_tag():
    assert TAG_EXCEPTION == "_exception"
