"""The small aggregate transport cannot change a funds operation or worker outcome."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from treg import analytics, worker
from treg.config import Settings
from treg.infra import kv, money_admission, money_admission_reporting as reporting


def test_rollout_is_opt_in_and_rejects_invalid_boundaries():
    settings = Settings(_env_file=None)
    assert not settings.money_admission_enabled
    assert settings.money_admission_org_ids == []
    for values in ({"money_admission_wait_s": 0}, {"money_admission_lease_s": 0},
                   {"money_admission_org_ids": [-1]}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)


def test_bounded_summary_reaches_both_transports(monkeypatch, caplog):
    row = {"operation": "close", "mode": "redis", "completed": 100, "wait_total_ms": 600}
    monkeypatch.setattr(money_admission, "snapshot", lambda: [row])
    monkeypatch.setattr(analytics, "build_id", lambda: "test-build")
    captured = []
    monkeypatch.setattr(analytics, "capture", lambda d, e, p: captured.append((e, p)))
    assert reporting.emit_snapshot(role="worker", shutdown=True) == 1
    assert captured == [("money_admission_gauge", {
        **row, "role": "worker", "shutdown": True, "build": "test-build"})]
    assert '"completed":100' in caplog.text
    assert len(caplog.records) == 1


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
