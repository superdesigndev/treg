"""Feedback reaches durable storage through HTTP, CLI and both MCP surfaces."""

import io
import json

import httpx
import pytest
from sqlmodel import select

from treg import cli
from treg.application import feedback as feedback_app
from treg.config import get_settings
from treg.feedback_contract import FEEDBACK_CATEGORIES
from treg.infra.db import session_maker
from treg.models import CallRecord, Feedback, FeedbackHandling, FeedbackHandlingEvent, LedgerEntry


async def rows():
    async with session_maker() as db:
        return list((await db.execute(select(Feedback).order_by(Feedback.id))).scalars())


async def test_feedback_is_durable_and_private_to_the_team(clients):
    response = await clients.post("/feedback", json={
        "category": "friction", "message": "  Pagination is unclear.  ",
    })
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["status"] == "received"
    report = (await clients.get(f'/feedback/{receipt["feedback_id"]}')).json()
    assert report["message"] == "Pagination is unclear."
    assert report["call_ids"] == []
    (row,) = await rows()
    assert row.id == receipt["feedback_id"]
    assert row.user_email == "tim@superdesign.dev"

    other = (await clients.post("/users", json={"email": "other-feedback@example.test"})).json()
    denied = await clients.get(f"/feedback/{row.id}", headers={"X-Treg-Token": other["token"]})
    assert denied.status_code == 404
    assert (await clients.get("/admin/feedback")).status_code == 403
    assert (await clients.post("/feedback", headers={"X-Treg-Token": ""}, json={
        "category": "other", "message": "Anonymous",
    })).status_code == 401


@pytest.mark.parametrize("body", [
    {"category": "rating", "message": "Five stars"},
    {"category": "other", "message": "   "},
    {"category": "other", "message": "x" * 2001},
    {"category": "other", "message": "Example", "raw_response": {"private": "data"}},
    {"category": "other", "message": "Example", "call_ids": ["person@example.test"]},
    {"category": "other", "message": "Example", "call_ids": ["id"] * 101},
    {"category": "other", "message": "Example", "endpoint_id": "https://private.example/path"},
])
async def test_invalid_feedback_is_not_stored(clients, body):
    assert (await clients.post("/feedback", json=body)).status_code == 422
    assert await rows() == []


async def test_call_references_verify_only_same_team_without_requiring_audit(clients):
    own = (await clients.post("/orgs", json={"name": "Feedback source"})).json()
    other = (await clients.post("/orgs", json={"name": "Other source"})).json()
    async with session_maker() as db:
        for org, ref in [(own, "same-team"), (other, "other-team")]:
            db.add(CallRecord(org_id=org["org_id"], user_email="tim@superdesign.dev",
                              tool_name="example", method="GET", path="/", status_code=200,
                              call_ref=ref))
        db.add(LedgerEntry(id="feedback-ledger-fixture", org_id=own["org_id"],
                           kind="settle", amount_micro=0, call_id="ledger-only"))
        await db.commit()
    response = await clients.post("/feedback", headers={"X-Treg-Token": own["token"]}, json={
        "category": "quality", "message": "Some results are outdated.",
        "call_ids": ["same-team", "other-team", "missing", "ledger-only", "same-team"],
    })
    assert response.status_code == 201, response.text
    (row,) = await rows()
    assert row.call_ids == ["same-team", "other-team", "missing", "ledger-only"]
    assert row.verified_call_ids == ["same-team", "ledger-only"]


async def test_rate_limit_is_per_team(clients, monkeypatch):
    monkeypatch.setattr(feedback_app, "RATE_MAX", 1)
    body = {"category": "other", "message": "A suggestion."}
    assert (await clients.post("/feedback", json=body)).status_code == 201
    assert (await clients.post("/feedback", json=body)).status_code == 429
    other = (await clients.post("/orgs", json={"name": "Separate feedback budget"})).json()
    assert (await clients.post("/feedback", json=body,
                               headers={"X-Treg-Token": other["token"]})).status_code == 201
    assert len(await rows()) == 2


async def test_failed_insert_rolls_back_without_an_acknowledgement(clients, monkeypatch):
    from treg.domain import feedback

    def fail(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(feedback_app, "RATE_MAX", 1)
    with monkeypatch.context() as patch:
        patch.setattr(feedback, "add", fail)
        with pytest.raises(RuntimeError, match="storage unavailable"):
            await clients.post("/feedback", json={"category": "other", "message": "Example"})
    assert await rows() == []
    assert (await clients.post("/feedback", json={
        "category": "other", "message": "Retry after recovery",
    })).status_code == 201


async def test_admin_can_filter_and_page_reports(clients, monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "feedback-admin-test-token")
    get_settings.cache_clear()
    try:
        for category in ["quality", "friction", "quality"]:
            await clients.post("/feedback", json={"category": category, "message": "Example"})
        headers = {"X-Treg-Token": "feedback-admin-test-token"}
        first = (await clients.get("/admin/feedback", headers=headers,
                                    params={"category": "quality", "limit": 1})).json()
        second = (await clients.get("/admin/feedback", headers=headers, params={
            "category": "quality", "limit": 1, "before": first["next_before"],
        })).json()
        assert first["items"][0]["id"] > second["items"][0]["id"]
        assert first["items"][0]["category"] == second["items"][0]["category"] == "quality"
    finally:
        get_settings.cache_clear()


async def test_feedback_docs_use_configured_base(clients, monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.test")
    get_settings.cache_clear()
    try:
        response = await clients.get("/feedback.md")
        assert response.status_code == 200
        assert "{BASE}" not in response.text
        assert "https://registry.example.test/feedback" in response.text
        skill = (await clients.get("/skill.md")).text
        assert "https://registry.example.test/feedback.md" in skill
    finally:
        get_settings.cache_clear()


def test_cli_feedback_sends_only_the_declared_fields(monkeypatch, capsys):
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(201, json={"feedback_id": 7, "status": "received"})

    monkeypatch.setattr(cli, "_client", lambda cfg: httpx.Client(
        transport=httpx.MockTransport(handle), base_url=cfg["base_url"],
    ))
    monkeypatch.setattr("sys.stdin", io.StringIO("A sanitized suggestion.\n"))
    args = cli.build_parser().parse_args([
        "feedback", "submit", "other", "-", "--call-id", "first", "--call-id", "second",
    ])
    args.fn(args, {"base_url": "https://self-hosted.example.test"})
    assert str(captured[0].url) == "https://self-hosted.example.test/feedback"
    assert json.loads(captured[0].content) == {
        "category": "other", "message": "A sanitized suggestion.\n", "call_ids": ["first", "second"],
    }
    assert json.loads(capsys.readouterr().out)["feedback_id"] == 7


@pytest.mark.parametrize("surface", ["team", "directory"])
async def test_both_mcp_surfaces_submit_to_the_same_intake(clients, surface):
    from test_mcp import _call_tool as team_call, mcp_session
    from test_mcp_directory import _call_tool as directory_call, directory_session

    token = clients.headers["X-Treg-Token"]
    context = mcp_session(clients) if surface == "team" else directory_session()
    call = team_call if surface == "team" else directory_call
    async with context as client:
        result = await call(client, "feedback", {
            "category": "pricing", "message": "The price unit is unclear.",
        }, token=token)
    assert result["status"] == "received", result
    (row,) = await rows()
    assert row.id == result["feedback_id"]
    assert row.category == "pricing"


async def test_mcp_exposes_the_small_category_enum():
    from treg.mcp import mcp, directory_mcp

    for server in [mcp, directory_mcp]:
        tool = next(tool for tool in await server.list_tools() if tool.name == "feedback")
        properties = tool.input_schema["properties"]
        assert properties["category"]["enum"] == list(FEEDBACK_CATEGORIES)
        assert tool.input_schema["required"] == ["category", "message"]
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False


async def test_deleting_a_team_removes_its_feedback(clients):
    org = (await clients.post("/orgs", json={"name": "Feedback cleanup"})).json()
    response = await clients.post("/feedback", headers={"X-Treg-Token": org["token"]}, json={
        "category": "other", "message": "A team-scoped suggestion.",
    })
    assert response.status_code == 201, response.text
    assert len(await rows()) == 1
    slug = next(item["slug"] for item in (await clients.get("/orgs")).json()
                if item["org_id"] == org["org_id"])
    deleted = await clients.delete(f'/orgs/{org["org_id"]}', params={"confirm": slug},
                                   headers={"X-Treg-Token": org["token"]})
    assert deleted.status_code == 200, deleted.text
    assert await rows() == []


async def test_public_demo_token_cannot_submit_feedback(clients):
    from test_public_demo import _mint_public, _org_with_stripe_tool

    org_id = await _org_with_stripe_tool(clients)
    token = await _mint_public(clients, org_id)
    response = await clients.post("/feedback", headers={"X-Treg-Token": token}, json={
        "category": "other", "message": "A report from a public token.",
    })
    assert response.status_code == 403, response.text
    assert await rows() == []


def test_cli_feedback_get_uses_the_configured_registry(monkeypatch, capsys):
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={"feedback_id": 7, "status": "received", "message": "Example"})

    monkeypatch.setattr(cli, "_client", lambda cfg: httpx.Client(
        transport=httpx.MockTransport(handle), base_url=cfg["base_url"],
    ))
    args = cli.build_parser().parse_args(["feedback", "get", "7"])
    args.fn(args, {"base_url": "https://self-hosted.example.test"})
    assert captured[0].method == "GET"
    assert str(captured[0].url) == "https://self-hosted.example.test/feedback/7"
    assert json.loads(capsys.readouterr().out)["message"] == "Example"


@pytest.mark.parametrize("status,code,hint", [
    (401, "authentication_required", "treg login"),
    (403, "access_denied", "active team"),
    (404, "not_found", "ID"),
    (422, "invalid_feedback", "submit --help"),
    (429, "rate_limited", "later"),
    (500, "submission_unconfirmed", "whether feedback was saved"),
])
def test_cli_feedback_errors_are_actionable_without_echoing_input(monkeypatch, capsys, status, code, hint):
    private = "private-person@example.test"
    monkeypatch.setattr(cli, "_client", lambda cfg: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={
            "detail": [{"input": private, "msg": private}],
        })), base_url="https://registry.example.test",
    ))
    args = cli.build_parser().parse_args(["feedback", "submit", "other", "A sanitized report."])
    with pytest.raises(SystemExit) as exc:
        args.fn(args, {})
    assert exc.value.code == 1
    output = capsys.readouterr()
    body = json.loads(output.out)
    assert body["error"] == code
    assert hint in body["message"]
    assert private not in output.out + output.err


@pytest.mark.parametrize("message,length", [("  ", 0), ("x" * 2001, 2001)])
def test_cli_feedback_validates_message_before_sending(monkeypatch, capsys, message, length):
    monkeypatch.setattr(cli, "_client", lambda cfg: pytest.fail("must not send invalid feedback"))
    args = cli.build_parser().parse_args(["feedback", "submit", "other", message])
    with pytest.raises(SystemExit):
        args.fn(args, {})
    body = json.loads(capsys.readouterr().out)
    assert body["error"] == "invalid_message"
    assert body["actual_length"] == length
    assert body["max_length"] == 2000


@pytest.mark.parametrize("action,code", [("submit", "submission_unconfirmed"), ("get", "request_failed")])
def test_cli_feedback_network_errors_do_not_claim_a_submission_failed(monkeypatch, capsys, action, code):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("private transport details", request=request)

    monkeypatch.setattr(cli, "_client", lambda cfg: httpx.Client(
        transport=httpx.MockTransport(handle), base_url="https://registry.example.test",
    ))
    tail = ["other", "Example"] if action == "submit" else ["7"]
    args = cli.build_parser().parse_args(["feedback", action, *tail])
    with pytest.raises(SystemExit):
        args.fn(args, {})
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == code
    assert "private transport details" not in output.out + output.err
    assert len(calls) == 1


def test_cli_feedback_without_arguments_shows_help_without_network(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda cfg: pytest.fail("help must not use the network"))
    args = cli.build_parser().parse_args(["feedback"])
    args.fn(args, {})
    output = capsys.readouterr().out
    assert "submit" in output and "get" in output
    for field in ("category", "message", "--call-id", "--endpoint-id", "feedback_id"):
        assert field in output
    assert "call_ids" in output and "not linked automatically" in output
    assert "1-2000" in output and "up to 100" in output


def test_cli_feedback_keeps_category_first_shorthand(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_load_config", lambda: {})
    monkeypatch.setattr(cli, "cmd_feedback", lambda args, cfg: calls.append((args.category, args.message)))
    cli.main(["feedback", "quality", "Example"])
    assert calls == [("quality", "Example")]


def test_cli_feedback_stdin_never_waits_for_interactive_input(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    args = cli.build_parser().parse_args(["feedback", "submit", "other", "-"])
    with pytest.raises(SystemExit):
        args.fn(args, {})
    assert json.loads(capsys.readouterr().out)["error"] == "stdin_required"


async def test_internal_handling_does_not_change_public_receipt(clients):
    receipt = (await clients.post("/feedback", json={
        "category": "other", "message": "Synthetic report",
    })).json()
    report_id = receipt["feedback_id"]
    before = (await clients.get(f"/feedback/{report_id}")).json()
    async with session_maker() as db:
        db.add(FeedbackHandling(feedback_id=report_id, status="resolved", version=1))
        db.add(FeedbackHandlingEvent(
            id="synthetic-internal-event", feedback_id=report_id, version=1,
            from_status="open", to_status="resolved", note="Internal result only",
            actor="shared-admin", source="api", links=["https://example.test/pull/1"],
        ))
        await db.commit()
    response = await clients.get(f"/feedback/{report_id}")
    assert response.status_code == 200
    assert response.json() == before
