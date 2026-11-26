"""The small aggregate transport cannot change a funds operation or worker outcome."""

from types import SimpleNamespace

import pytest

from treg import analytics, worker
from treg.infra import kv, money_admission, money_admission_reporting as reporting


def test_analytics_failure_retains_local_summary(monkeypatch, caplog):
    monkeypatch.setattr(money_admission, "snapshot", lambda: [{"completed": 1}])

    def broken(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(analytics, "capture", broken)
    assert reporting.emit_snapshot(role="web") == 1
    assert "money_admission_gauge" in caplog.text
    monkeypatch.setattr(money_admission, "snapshot", broken)
    assert reporting.emit_snapshot(role="web") == 0


async def test_worker_reports_and_drains_after_command_even_on_failure(monkeypatch):
    events = []

    def emit(**kwargs):
        events.append(("report", kwargs))
        return 1

    async def drain():
        events.append("drain")

    async def close():
        events.append("close")

    failure = RuntimeError("command failed")

    async def command(args):
        events.append("command")
        raise failure

    monkeypatch.setattr(reporting, "emit_snapshot", emit)
    monkeypatch.setattr(analytics, "drain", drain)
    monkeypatch.setattr(kv, "close", close)
    with pytest.raises(RuntimeError) as caught:
        await worker._run_command(SimpleNamespace(fn=command))
    assert caught.value is failure
    assert events == ["command", ("report", {"role": "worker", "shutdown": True}),
                      "close", "drain"]
