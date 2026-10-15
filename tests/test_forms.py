"""The forms a person actually fills in, and the promise that no secret leaks."""

import json

from codex_agent import forms


def _schema(form):
    return json.loads(form.build().jsonschema)


def test_both_forms_build():
    # build() validates; a malformed form is a start-up error, not a dead dialog.
    assert _schema(forms.run_form())["properties"]
    assert _schema(forms.settings_form())["properties"]


def test_the_task_is_the_only_required_field():
    schema = _schema(forms.run_form())
    assert schema.get("required") == ["prompt"]


def test_the_run_form_carries_no_credential_field():
    """The node must never offer a place to type a key: the profile holds it.

    Field NAMES are what the host sends and stores, so those are what this
    asserts — the prose may well mention tokens of the ``{{$.path}}`` kind.
    """
    names = [n.lower() for n in _schema(forms.run_form())["properties"]]
    # "token" alone would catch maxTokens, which is a budget, not a credential.
    for forbidden in ("apikey", "api_key", "apitoken", "accesstoken", "secret", "password", "credential"):
        assert not any(forbidden in n for n in names)
    # …and no control on it is rendered as a password field.
    ui = json.dumps(forms.run_form().build().jsonui)
    assert "password" not in ui


def test_the_key_is_rendered_as_a_password_on_the_settings_form():
    assert "apiKey" in _schema(forms.settings_form())["properties"]
    ui = json.loads(forms.settings_form().build().jsonui)
    control = next(e for e in ui["elements"] if e.get("scope", "").endswith("/apiKey"))
    assert control["options"]["format"] == "password"


def test_sandbox_offers_exactly_the_three_the_sdk_knows():
    sandbox = _schema(forms.run_form())["properties"]["sandbox"]
    offered = {o["const"] for o in sandbox["oneOf"]}
    assert offered == {"workspace-write", "read-only", "danger-full-access"}
    assert sandbox["default"] == "workspace-write"
    # Every value the form offers must map onto a real SDK sandbox.
    from codex_agent.agent import sandbox_of

    assert all(sandbox_of(v) is not None for v in offered)


def test_the_answer_path_defaults_to_the_human_in_the_loop_key():
    props = _schema(forms.run_form())["properties"]
    assert props["answerFrom"]["default"] == forms.DEFAULT_ANSWER_PATH


def test_every_meta_the_forms_call_is_a_method_the_plugin_serves():
    from codex_agent.metas import metas

    served = {m.method for m in metas()}
    ui = json.dumps(forms.run_form().build().jsonui) + json.dumps(forms.settings_form().build().jsonui)
    for method in (forms.META_PING, forms.META_MODELS, forms.META_DIRS):
        assert method in served
        assert method in ui
