"""{{$.path}} resolution, against a job that only answers scope reads."""

import pytest

from codex_agent.vars import Resolver, read_scope


class FakeJob:
    def __init__(self, values: dict):
        self.values = values
        self.reads: list[str] = []

    async def cmd_get_scope(self, path: str):
        self.reads.append(path)
        if path not in self.values:
            return Exception("no such path")
        v = self.values[path]
        import json

        return json.dumps(v).encode()


async def test_a_string_is_substituted_raw():
    job = FakeJob({"$.clone.path": "/srv/ws/repo"})
    assert await Resolver(job).resolve("work in {{$.clone.path}}") == "work in /srv/ws/repo"


async def test_an_object_is_substituted_as_compact_json():
    job = FakeJob({"$.issue": {"id": 7}})
    assert await Resolver(job).resolve("{{$.issue}}") == '{"id":7}'


async def test_an_unreadable_path_leaves_its_token_visible():
    job = FakeJob({})
    # A typo must show up in the prompt rather than become a blank instruction.
    assert await Resolver(job).resolve("see {{$.nope}}") == "see {{$.nope}}"


async def test_each_path_is_read_once_however_often_it_appears():
    job = FakeJob({"$.a": "x"})
    await Resolver(job).resolve("{{$.a}} {{$.a}} {{$.a}}")
    assert job.reads == ["$.a"]


async def test_text_without_tokens_never_touches_the_runtime():
    job = FakeJob({})
    assert await Resolver(job).resolve("plain task") == "plain task"
    assert job.reads == []


async def test_read_scope_returns_none_rather_than_raising():
    assert await read_scope(FakeJob({}), "$.missing") is None
    assert await read_scope(FakeJob({"$.a": {"k": 1}}), "$.a") == {"k": 1}
