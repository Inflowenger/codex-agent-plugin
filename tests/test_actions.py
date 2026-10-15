"""The action's own decisions: what is a thread id, what a schema must be, and
which config overrides a node implies."""

from codex_agent.actions import is_thread_id, overrides_for, _parse_schema, _positive
from codex_agent.settings import Conn


def test_a_codex_thread_id_is_usable_as_a_job_id():
    # Verified against a live runtime: thread ids are UUIDv7.
    assert is_thread_id("01a14062-3976-72a3-bb22-da03434624e4")


def test_fractal_cores_job_id_rules_are_enforced():
    assert not is_thread_id("short")            # under 10 characters
    assert not is_thread_id("has.a.dot")        # not a usable NATS subject token
    assert not is_thread_id("has space")
    assert not is_thread_id("star*")
    assert not is_thread_id(None)


def test_web_search_is_turned_off_for_minimal_effort():
    # The API refuses the combination outright, so the node must not send it.
    assert 'web_search="disabled"' in overrides_for(Conn(), {"effort": "minimal"})


def test_an_explicit_web_search_choice_is_respected():
    assert 'web_search="live"' in overrides_for(Conn(), {"webSearch": "live"})


def test_network_access_is_a_sandbox_config_key():
    assert "sandbox_workspace_write.network_access=false" in overrides_for(Conn(), {"networkAccess": False})
    assert "sandbox_workspace_write.network_access=true" in overrides_for(Conn(), {"networkAccess": True})


def test_an_unset_network_switch_leaves_the_hosts_default_alone():
    assert not any("network_access" in o for o in overrides_for(Conn(), {}))


def test_the_profiles_base_url_rides_along():
    assert 'openai_base_url="https://gw"' in overrides_for(Conn(base_url="https://gw"), {})


def test_a_schema_must_be_a_json_object():
    assert _parse_schema('{"type":"object"}') == {"type": "object"}
    assert isinstance(_parse_schema(""), str)
    assert isinstance(_parse_schema("not json"), str)
    assert isinstance(_parse_schema("[1,2]"), str)


def test_budgets_below_one_mean_no_ceiling():
    assert _positive(0) == 0 and _positive(-5) == 0 and _positive("x") == 0
    assert _positive(2000) == 2000
