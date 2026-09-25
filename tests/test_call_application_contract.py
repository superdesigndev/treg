"""Executable contract for the Stage 4 call application boundary."""

from treg.application.call import authorize


async def test_authorization_gate_order_is_frozen(monkeypatch) -> None:
    order = []

    class Session:
        async def commit(self):
            order.append("commit")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(authorize, "session_maker", SessionContext)
    monkeypatch.setattr(
        authorize.access_policy, "_require_tool_use", lambda caller, tool: order.append("acl"))

    async def deny(*args):
        order.append("deny")

    async def daily(*args, **kwargs):
        order.append("daily")

    async def public(*args):
        order.append("public")

    monkeypatch.setattr(authorize.access_policy, "enforce_deny", deny)
    monkeypatch.setattr(authorize.usage_policy, "enforce_daily_cap", daily)
    monkeypatch.setattr(authorize.publicdemo_policy, "enforce_public_demo_ip_cap", public)
    monkeypatch.setattr(authorize.demo_sandbox, "is_sandbox", lambda org: False)
    caller = type("Caller", (), {
        "role": "member",
        "org": type("Org", (), {"public_demo": True})(),
    })()
    tool = type("Tool", (), {"project_id": None})()

    await authorize.authorize_call(
        caller=caller, tool=tool, upstream_url="https://upstream.test/x",
        method="GET", client_ip="203.0.113.1")

    assert order == ["acl", "deny", "daily", "public", "commit"]


