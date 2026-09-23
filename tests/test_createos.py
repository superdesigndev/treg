"""CreateOS catalog and lifecycle calls use the customer's key and no treg balance."""

from __future__ import annotations

import dataclasses
import json

import httpx
from httpx import AsyncClient

from treg import oauth_providers
from treg.api import app


class JSONStream(httpx.AsyncByteStream):
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode()

    async def __aiter__(self):
        yield self.body


def response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "application/json"}, stream=JSONStream(body))


async def test_create_exec_delete_use_own_key_without_treg_charge(
    clients: AsyncClient, monkeypatch,
) -> None:
    seen: list[tuple[str, str, str]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("x-api-key", "")))
        if request.url.path == "/v1/whoami":
            return response(200, {"status": "success", "data": {"id": "customer"}})
        if request.method == "GET" and request.url.path == "/v1/shapes":
            return response(200, {"status": "success", "data": {"data": [{"id": "s-1vcpu-1gb", "vcpu": 1, "mem_mib": 1024}]}})
        if request.method == "GET" and request.url.path == "/v1/rootfs":
            return response(200, {"status": "success", "data": {"rootfs": ["devbox:1"], "default": "devbox:1"}})
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return response(201, {"status": "success", "data": {"id": "sb-test"}})
        if request.method == "GET" and request.url.path == "/v1/sandboxes":
            assert request.url.params["limit"] == "10"
            assert request.url.params["status"] == "running"
            return response(200, {"status": "success", "data": {"data": [{"id": "sb-test", "status": "running"}], "pagination": {"total": 1, "limit": 10, "offset": 0, "count": 1}}})
        if request.method == "GET" and request.url.path == "/v1/sandboxes/sb-test":
            return response(200, {"status": "success", "data": {"id": "sb-test", "status": "running"}})
        if request.method == "PATCH" and request.url.path == "/v1/sandboxes/sb-test":
            assert json.loads(request.content) == {"auto_pause_after_seconds": 60}
            return response(200, {"status": "success", "data": {"id": "sb-test", "status": "running", "auto_pause_after_seconds": 60}})
        if request.method == "POST" and request.url.path == "/v1/sandboxes/sb-test/exec":
            return response(200, {"status": "success", "data": {"result": {"exit_code": 0, "stdout": "hello"}}})
        if request.method == "DELETE" and request.url.path == "/v1/sandboxes/sb-test":
            return response(200, {"status": "success", "data": {"status": "destroying"}})
        return response(404, {"status": "fail"})

    monkeypatch.setitem(oauth_providers.REGISTRY, "createos", dataclasses.replace(
        oauth_providers.CREATEOS, base_url="http://upstream"))
    original_http = app.state.http
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream") as upstream_client:
        app.state.http = upstream_client
        try:
            connected = await clients.post("/connections/token", json={"provider": "createos", "token": "customer-key"})
            assert connected.status_code == 200, connected.text

            shapes = await clients.get("/call/createos.sandbox.shapes.list")
            assert shapes.status_code == 200, shapes.text
            assert shapes.json()["data"]["data"][0]["id"] == "s-1vcpu-1gb"

            rootfs = await clients.get("/call/createos.sandbox.rootfs.list")
            assert rootfs.status_code == 200, rootfs.text
            assert rootfs.json()["data"]["default"] == "devbox:1"

            create = await clients.post("/call/createos.sandbox.create", json={"shape": "s-1vcpu-1gb", "rootfs": "devbox:1"})
            assert create.status_code == 201, create.text
            assert create.json()["data"]["id"] == "sb-test"

            listed = await clients.get("/call/createos.sandbox.list?status=running&limit=10")
            assert listed.status_code == 200, listed.text
            assert listed.json()["data"]["data"][0]["id"] == "sb-test"

            got = await clients.get("/call/createos.sandbox.get?id=sb-test")
            assert got.status_code == 200, got.text
            assert got.json()["data"]["id"] == "sb-test"

            updated = await clients.patch("/call/createos.sandbox.update?id=sb-test", json={"auto_pause_after_seconds": 60})
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["auto_pause_after_seconds"] == 60

            execute = await clients.post("/call/createos.sandbox.exec?id=sb-test", json={"cmd": "printf", "args": ["hello"]})
            assert execute.status_code == 200, execute.text
            assert execute.json()["data"]["result"]["exit_code"] == 0

            delete = await clients.delete("/call/createos.sandbox.delete?id=sb-test")
            assert delete.status_code == 200, delete.text
            assert delete.json()["data"]["status"] == "destroying"

            assert [method for method, _, _ in seen] == ["GET", "GET", "GET", "POST", "GET", "GET", "PATCH", "POST", "DELETE"]
            assert all(key == "customer-key" for _, _, key in seen)
            assert all(response.headers.get("x-treg-cost-micro") in (None, "0")
                       for response in (shapes, rootfs, create, listed, got, updated, execute, delete))
        finally:
            app.state.http = original_http
