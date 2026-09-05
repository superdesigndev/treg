"""PR-B contracts for AIGC providers, ingest, relay metadata, and CLI waiting."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import yaml

from treg import cli
from treg.application.call.types import UpstreamResponse
from treg.domain import asynctasks
from treg.domain.catalog import store as catalog_store
from treg.routers import call as call_router


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.events: list[str] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def report(self, message: str) -> None:
        self.events.append(message)


def response(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers,
                          request=httpx.Request("GET", "https://example.test"))


def static_descriptor(*, fetch: bool = False) -> dict:
    result = ({"fetch": "vendor.result", "fetch_param": {
        "in": "pathParams", "name": "file_id", "value_from": "file.id"},
               "ttl_note": "9h"} if fetch else {"path": "task.content.url"})
    return {
        "id_from": "task_id",
        "poll": {"endpoint": "vendor.task.status",
                 "param": {"in": "queryParams", "name": "task_id"}},
        "status": {"path": "task.status", "success": ["succeeded"],
                   "failure": ["failed", "cancelled"]},
        "result": result,
        "interval": 2,
    }


def test_catalog_loads_provider_defaults_and_utility_opt_out():
    cat = catalog_store.load()
    submit = cat.by_id["minimax.video-gen.from_text"]
    status = cat.by_id["minimax.video-gen.task.status"]
    assert submit["async"]["poll"]["endpoint"] == status["id"]
    assert status["async"] is None
    replicate = cat.by_id["replicate.image-gen.flux-schnell"]["async"]["poll"]
    assert replicate == {"endpoint": "replicate.predictions.get", "param": {"in": "pathParams", "name": "id"}}


def test_openrouter_cancelled_and_expired_jobs_are_terminal_failures():
    cat = catalog_store.load()
    endpoint_ids = (
        "openrouter.video-gen.wan-3-0.from_text",
        "openrouter.x.alibaba-happyhorse-1-0",
    )
    for endpoint_id in endpoint_ids:
        descriptor = cat.by_id[endpoint_id]["async"]
        for status in ("failed", "cancelled", "expired"):
            assert asynctasks.classify_terminal(descriptor, {"status": status}) == "failure"


def test_async_wait_succeeds_after_unknown_status_and_warns_once():
    replies = iter([
        response(200, {"task": {"status": "new-provider-state"}}),
        response(200, {"task": {"status": "new-provider-state"}}),
        response(200, {"task": {"status": "succeeded", "content": {"url": "https://asset.test/v.mp4"}}}),
    ])
    clock = FakeClock()
    outcome = cli.await_async_task(
        static_descriptor(), response(202, {"task_id": "task-1"}),
        lambda target, params: next(replies), clock, 30,
    )
    assert outcome["code"] == 0
    assert outcome["result"] == "https://asset.test/v.mp4"
    assert outcome["recovery"] == "treg call vendor.task.status -p task_id=task-1"
    assert sum("unknown async status" in event for event in clock.events) == 1


def test_async_wait_returns_terminal_failure_verbatim_to_caller():
    terminal = response(200, {"task": {"status": "failed", "error": "moderated"}})
    outcome = cli.await_async_task(
        static_descriptor(), response(202, {"task_id": "task-2"}),
        lambda target, params: terminal, FakeClock(), 30,
    )
    assert outcome["code"] == 2
    assert outcome["response"] is terminal


def test_async_wait_retries_network_and_server_errors_five_times():
    clock = FakeClock()
    attempts = 0

    def failing_call(target, params):
        nonlocal attempts
        attempts += 1
        if attempts % 2:
            raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://example.test"))
        return response(503, {"error": "busy"})

    outcome = cli.await_async_task(
        static_descriptor(), response(202, {"task_id": "task-3"}), failing_call, clock, 300,
    )
    assert outcome["code"] == 3
    assert attempts == 5
    assert clock.sleeps == [2, 2, 4, 8, 16]


def test_async_wait_timeout_is_recoverable():
    outcome = cli.await_async_task(
        static_descriptor(), response(202, {"task_id": "task-4"}),
        lambda target, params: response(200, {"task": {"status": "processing"}}),
        FakeClock(), 3,
    )
    assert outcome["code"] == 3
    assert "timed out" in outcome["error"]
    assert "task_id=task-4" in outcome["recovery"]


def test_async_wait_dynamic_url_enforces_hosts_and_fetches_by_terminal_id():
    descriptor = static_descriptor(fetch=True)
    descriptor["poll"] = {"url_from": "urls.get", "url_hosts": ["api.replicate.com"]}
    terminal = response(200, {"task": {"status": "succeeded"}, "file": {"id": "file-9"}})
    outcome = cli.await_async_task(
        descriptor,
        response(202, {"task_id": "task-5", "urls": {"get": "https://api.replicate.com/v1/predictions/task-5"}}),
        lambda target, params: terminal,
        FakeClock(), 30,
    )
    assert outcome["code"] == 0
    assert outcome["fetch_command"] == "treg call vendor.result -p file_id=file-9"
    assert outcome["ttl_note"] == "9h"

    refused = cli.await_async_task(
        descriptor,
        response(202, {"task_id": "task-6", "urls": {"get": "https://evil.example/task-6"}}),
        lambda target, params: terminal,
        FakeClock(), 30,
    )
    assert refused["code"] == 1
    assert "allow-list" in refused["error"]


def call_args(**overrides):
    values = {
        "query": [], "upload": [], "file": None, "data": None, "content_type": None,
        "header": [], "target": "vendor.submit", "path": "", "method": "POST",
        "await_task": True, "timeout": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_call_await_is_a_noop_without_the_async_header(monkeypatch, capsys):
    submitted = response(200, {"synchronous": True})

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def request(self, *args, **kwargs): return submitted

    monkeypatch.setattr(cli, "_client", lambda cfg: Client())
    cli.cmd_call(call_args(), {})
    assert json.loads(capsys.readouterr().out) == {"synchronous": True}


def test_call_preserves_binary_result_bytes(monkeypatch, capsysbinary):
    payload = b"\x00\xffvideo\r\n"
    result = httpx.Response(200, content=payload, headers={"content-type": "video/mp4"})

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def request(self, *args, **kwargs): return result

    monkeypatch.setattr(cli, "_client", lambda cfg: Client())
    cli.cmd_call(call_args(await_task=False, method="GET"), {})
    assert capsysbinary.readouterr().out == payload


def test_call_await_writes_only_terminal_raw_body_to_stdout(monkeypatch, capsys):
    descriptor = static_descriptor(fetch=True)
    submitted = response(202, {"task_id": "task-7"}, {
        "X-Treg-Async": json.dumps(descriptor), "X-Treg-Cost-Micro": "100000",
    })
    terminal = response(200, {"task": {"status": "succeeded"}, "file": {"id": "file-7"}})

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def request(self, *args, **kwargs): return submitted
        def get(self, *args, **kwargs): return terminal

    monkeypatch.setattr(cli, "_client", lambda cfg: Client())
    monkeypatch.setattr(cli, "time", FakeClock())
    try:
        cli.cmd_call(call_args(), {})
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()
    assert captured.out == terminal.text
    assert "async task submitted: task-7" in captured.err
    assert "resume: treg call vendor.task.status -p task_id=task-7" in captured.err
    assert "generation reservation: $0.1" in captured.err
    assert "retrieve the result (file bytes, or JSON with a download URL): treg call vendor.result -p file_id=file-7" in captured.err
    assert "result lifetime: 9h" in captured.err


def test_call_await_routes_dynamic_poll_urls_back_through_treg(monkeypatch, capsys):
    descriptor = static_descriptor()
    descriptor["poll"] = {"url_from": "urls.get", "url_hosts": ["api.replicate.com"]}
    submitted = response(202, {
        "task_id": "task-8", "urls": {"get": "https://api.replicate.com/v1/predictions/task-8"},
    }, {"X-Treg-Async": json.dumps(descriptor)})
    terminal = response(200, {"task": {"status": "succeeded", "content": {"url": "asset"}}})
    called = []

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def request(self, *args, **kwargs): return submitted
        def get(self, target, **kwargs):
            called.append(target)
            return terminal

    monkeypatch.setattr(cli, "_client", lambda cfg: Client())
    monkeypatch.setattr(cli, "time", FakeClock())
    try:
        cli.cmd_call(call_args(), {})
    except SystemExit as exc:
        assert exc.code == 0
    assert called == ["/call/https://api.replicate.com/v1/predictions/task-8"]
    assert capsys.readouterr().out == terminal.text


def test_router_attaches_static_async_metadata_without_reading_body(monkeypatch):
    descriptor = static_descriptor()
    monkeypatch.setattr(call_router.catalog_store, "load", lambda: SimpleNamespace(
        by_id={"vendor.submit": {"async": descriptor}}
    ))

    async def body():
        raise AssertionError("attaching metadata must not consume the body")
        yield b""

    async def close():
        return None

    upstream = UpstreamResponse(202, ((b"content-type", b"application/json"),), body(), close)
    context = SimpleNamespace(marketplace=SimpleNamespace(endpoint_id="vendor.submit"))
    call_router._attach_async_descriptor(upstream, context)
    headers = httpx.Headers(upstream.raw_headers)
    assert json.loads(headers["X-Treg-Async"]) == descriptor


def test_ingesters_are_byte_deterministic(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts" / "catalog_ingest.py"
    spec = importlib.util.spec_from_file_location("catalog_ingest_pr_b", script)
    assert spec and spec.loader
    ingest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingest)

    monkeypatch.setattr(ingest, "CATALOG", tmp_path)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "placeholder-token")
    (tmp_path / "openrouter.yaml").write_text(
        "provider: openrouter\nendpoints:\n"
        "  - id: openrouter.video-gen.curated\n    method: POST\n    path: /videos\n"
        "    input:\n      body:\n        model: {type: string, enum: [vendor/curated]}\n")
    (tmp_path / "replicate.yaml").write_text("provider: replicate\nendpoints: []\n")
    (tmp_path / "openrouter.extended.yaml").write_text(
        "provider: openrouter\nendpoints:\n"
        "  - id: openrouter.x.minimax-hailuo-test\n    method: POST\n    path: /videos\n"
        "    capability: video-gen.from_text\n    platform: video-gen\n"
        "    verified: '2026-09-02'\n    example_response: examples/openrouter.json\n")
    (tmp_path / "replicate.extended.yaml").write_text(
        "provider: replicate\nendpoints:\n"
        "  - id: replicate.x.vendor-video-model\n    method: POST\n"
        "    path: /models/vendor/video-model/predictions\n"
        "    capability: video-gen.from_text\n    platform: video-gen\n"
        "    verified: '2026-09-02'\n    example_response: examples/replicate.json\n")
    openrouter_payload = {"data": [
        {
            "id": "minimax/hailuo-test", "name": "Hailuo Test",
            "description": "Text-to-video test model", "supported_durations": [5],
            "supported_resolutions": ["720p"], "supported_aspect_ratios": ["16:9"],
            "generate_audio": True,
            "pricing_skus": {
                "cents_per_video_output_second_720p_with_audio": "10",
                "cents_per_video_output_second_720p_without_audio": "5",
                "minimum_cents_per_generation": "25",
            },
        },
        {
            "id": "vendor/unbounded", "name": "Unbounded", "description": "Video model",
            "pricing_skus": {"cents_per_image_input": "2"},
        },
        {
            "id": "vendor/curated", "name": "Curated", "description": "Core model",
            "pricing_skus": {"generate": "1"},
        },
    ]}
    replicate_model = {
        "owner": "vendor", "name": "video-model", "is_official": True,
        "description": "A video model", "url": "https://replicate.com/vendor/video-model",
        "latest_version": {"openapi_schema": {"components": {"schemas": {"Input": {
            "required": ["prompt"], "properties": {"prompt": {"type": "string", "x-order": 0}}
        }}}}},
    }

    def fake_fetch(url, key, **kwargs):
        payload = openrouter_payload if "openrouter" in url else {"models": [replicate_model]}
        return json.dumps(payload).encode()

    monkeypatch.setattr(ingest, "fetch", fake_fetch)
    openrouter_path, _ = ingest.ingest_openrouter(False)
    first_openrouter = openrouter_path.read_bytes()
    ingest.ingest_openrouter(False)
    assert openrouter_path.read_bytes() == first_openrouter
    openrouter_doc = yaml.safe_load(openrouter_path.read_text())
    priced = next(ep for ep in openrouter_doc["endpoints"] if ep["id"].endswith("hailuo-test"))
    assert priced["input"]["body"]["generate_audio"]["default"] is True
    assert [row["value"] for row in priced["cost"]["table"]] == [0.05, 0.1]
    assert priced["cost"]["fallback"]["value"] >= 0.5
    assert priced["cost"]["confidence"] == "documented"
    assert priced["domain"] == "models"
    assert "capability" not in priced
    assert priced["verified"] == "2026-09-02"
    assert priced["example_response"] == "examples/openrouter.json"
    unbounded = next(ep for ep in openrouter_doc["endpoints"] if ep["id"].endswith("unbounded"))
    assert unbounded["cost"]["confidence"] == "unknown"
    assert unbounded["cost"]["value"] is None
    assert not any(ep["id"].endswith("curated") for ep in openrouter_doc["endpoints"])

    replicate_path, _ = ingest.ingest_replicate(False)
    first_replicate = replicate_path.read_bytes()
    ingest.ingest_replicate(False)
    assert replicate_path.read_bytes() == first_replicate
    doc = yaml.safe_load(replicate_path.read_text())
    generated = doc["endpoints"][0]
    input_spec = generated["input"]["body"]["input"]
    assert input_spec["properties"]["prompt"]["required"] is True
    assert generated["domain"] == "models"
    assert "capability" not in generated
    assert generated["verified"] == "2026-09-02"
    assert generated["example_response"] == "examples/replicate.json"
