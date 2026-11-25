"""Every door that stores a name a team chose refuses control characters and unbounded length."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

BASE = "https://api.stripe.com/v1"
BAD = ["", "x\ntouch /tmp/pwned\n#", "x\ry", "a‮b", "A" * 129]


@pytest.mark.parametrize("name", BAD)
async def test_a_tool_name_with_a_newline_or_no_bound_is_refused(clients: AsyncClient, name: str):
    r = await clients.post("/tools", json={"name": name, "base_url": BASE})
    assert r.status_code == 422, r.text
    assert (await clients.get("/tools")).json() == []


async def test_names_teams_already_use_still_register(clients: AsyncClient):
    for name in ("My API · prod", "stripe.charges", "A" * 128):
        r = await clients.post("/tools", json={"name": name, "base_url": BASE})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == name


@pytest.mark.parametrize("name", BAD)
async def test_a_secret_name_is_held_to_the_same_rule(clients: AsyncClient, name: str):
    r = await clients.post("/secrets", json={"name": name, "value": "v"})
    assert r.status_code == 422, r.text


async def test_a_skill_carrying_a_bad_tool_name_registers_nothing(clients: AsyncClient):
    r = await clients.post("/skills", json={
        "name": "ok-skill", "tools": [{"name": "x\ntouch /tmp/pwned", "base_url": BASE}]})
    assert r.status_code == 422, r.text
    assert (await clients.get("/tools")).json() == []


async def test_an_uploaded_skill_with_a_bad_name_says_why(clients: AsyncClient):
    contract = json.dumps({
        "name": "x", "base_url": BASE,
        "secrets": [{"file": ".secrets/token", "name": "key", "kind": "env"}],
        "bindings": [{"secret": "key", "injector": "env", "location": "header",
                      "name": "Authorization", "format": "Bearer {secret}"}],
    })
    folder = "A" * 129
    files = [{"path": f"{folder}/SKILL.md", "content": "# x"},
             {"path": f"{folder}/treg.json", "content": contract},
             {"path": f"{folder}/.secrets/token", "content": "TOKEN"}]
    r = await clients.post("/skills/import", json={"files": files})
    assert r.status_code == 200, r.text
    [result] = r.json()["results"]
    assert result["ok"] is False and "1-128 characters" in result["error"]
    assert (await clients.get("/tools")).json() == []
