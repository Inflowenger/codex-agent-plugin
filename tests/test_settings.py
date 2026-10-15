"""The fence, and the profile that defines it. No platform, no Codex, no spend."""

from pathlib import Path

import pytest

from codex_agent.settings import (
    Conn,
    agent_env,
    config_overrides,
    contains,
    is_git_repo,
    parse_settings,
    resolve_workspace,
)


def test_keys_are_matched_leniently():
    conn = parse_settings({"API Key": " sk-abc ", "base-url": "https://gw", "Workspace_Root": "/srv/ws"})
    assert conn.api_key == "sk-abc"
    assert conn.base_url == "https://gw"
    assert conn.workspace_root == "/srv/ws"


def test_empty_values_are_not_picked_up():
    assert parse_settings({"apiKey": "   ", "model": None}) == Conn()


def test_env_spreads_the_parent_or_the_subprocess_cannot_run():
    env = agent_env(Conn(api_key="sk-1"), base={"PATH": "/usr/bin", "HOME": "/home/x"})
    assert env["PATH"] == "/usr/bin"       # replaced, not merged, by the SDK
    assert env["CODEX_API_KEY"] == "sk-1"
    assert env["CI"] == "1"                # never open a browser on a headless host


def test_no_key_leaves_the_hosts_own_login_alone():
    assert "CODEX_API_KEY" not in agent_env(Conn(), base={"PATH": "/usr/bin"})


def test_base_url_is_a_config_override_not_an_env_var():
    assert config_overrides(Conn(base_url="https://gw")) == ['openai_base_url="https://gw"']
    assert config_overrides(Conn()) == []


def test_prefix_tricks_do_not_pass_the_fence():
    assert contains("/srv/ws", "/srv/ws/a")
    assert contains("/srv/ws", "/srv/ws")
    assert not contains("/srv/ws", "/srv/ws-evil")
    assert not contains("/srv/ws", "/srv")


def test_a_workspace_outside_the_root_fails_with_the_fix_in_the_message(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    where = resolve_workspace(str(outside), Conn(workspace_root=str(root)))
    assert not where.ok
    assert "outside the allowed root" in where.error


def test_an_empty_workspace_means_the_root(tmp_path):
    (tmp_path / "ws").mkdir()
    conn = Conn(workspace_root=str(tmp_path / "ws"))
    assert resolve_workspace("", conn).cwd == str(tmp_path / "ws")


def test_a_missing_directory_is_named(tmp_path):
    where = resolve_workspace(str(tmp_path / "nope"), Conn(workspace_root=str(tmp_path)))
    assert not where.ok and "does not exist" in where.error


def test_no_root_anywhere_is_an_error_not_a_guess():
    assert not resolve_workspace("", Conn(), fallback_root="").ok


def test_git_detection_sees_a_parent_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert is_git_repo(deep)
    assert not is_git_repo(Path("/"))
