"""Application orchestration for organization-scoped provider resources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from ..domain import provider_resources
from ..domain.identity.access import Caller
from ..infra.db import session_maker
from .call.access import catalog_endpoint_access
from .call.service import create_call_context, execute_call
from .call.types import CallFailure, CallInput, CallerSnapshot, UpstreamResponse


_RESOURCE_RESPONSE_MAX = 8 * 1024 * 1024
_FISH_PAGE_SIZE = 100
_FISH_MAX_PAGES = 100


class _BytesBody:
    async def read(self) -> bytes:
        return b""

    async def stream(self):
        if False:
            yield b""


@dataclass(frozen=True)
class ResourceList:
    source: str
    resources: list[dict]


class ResourceListFailed(Exception):
    def __init__(self, status_code: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _fish_voice_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next(
            (payload[key] for key in ("items", "models", "results", "data")
             if isinstance(payload.get(key), list)),
            [],
        )
    else:
        rows = []
    return [{
        "id": row.get("_id") or row.get("id") or row.get("model_id"),
        "provider": "fishaudio",
        "kind": "voice",
        "upstream_id": row.get("_id") or row.get("id") or row.get("model_id"),
        "display_name": row.get("title") or row.get("name") or "Untitled voice",
        "created_by": None,
        "source_call_id": None,
        "status": "active",
        "created_at": row.get("created_at") or row.get("createdAt"),
        "updated_at": row.get("updated_at") or row.get("updatedAt"),
        "deleted_at": None,
    } for row in rows if isinstance(row, dict) and (
        row.get("_id") or row.get("id") or row.get("model_id")
    )]


async def _read_json(response: UpstreamResponse) -> object:
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in response.body_stream:
            raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
            size += len(raw)
            if size > _RESOURCE_RESPONSE_MAX:
                raise ResourceListFailed(502, {
                    "error": "response_buffer_limit",
                    "message": "provider resource response exceeds treg's 8 MiB buffer",
                })
            chunks.append(raw)
    finally:
        await response.close()
    body = b"".join(chunks)
    try:
        payload = json.loads(body) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResourceListFailed(502, "provider returned an invalid resource list") from exc
    if response.status >= 400:
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        raise ResourceListFailed(response.status, detail)
    return payload


async def _fish_account_voices(
    caller: Caller,
    upstream_client: httpx.AsyncClient,
    *,
    client_ip: str,
    client_name: str,
) -> list[dict]:
    raw_headers = (
        ((b"x-treg-client", client_name.encode("latin-1", "replace")),)
        if client_name else ()
    )
    resources: list[dict] = []
    for page_number in range(1, _FISH_MAX_PAGES + 1):
        query_items = (
            ("self", "true"),
            ("page_size", str(_FISH_PAGE_SIZE)),
            ("page_number", str(page_number)),
        )
        call_input = CallInput(
            method="GET",
            raw_rest="fishaudio.voices.list",
            raw_headers=raw_headers,
            query_items=query_items,
            raw_query=urlencode(query_items),
            body=_BytesBody(),
            caller=CallerSnapshot.capture(caller),
            client_ip=client_ip,
        )
        try:
            upstream = await execute_call(create_call_context(call_input), upstream_client)
        except CallFailure as exc:
            raise ResourceListFailed(exc.status_code, exc.detail) from exc
        payload = await _read_json(upstream)
        rows = _fish_voice_rows(payload)
        resources.extend(rows)
        has_more = payload.get("has_more") if isinstance(payload, dict) else None
        if has_more is False or (has_more is None and len(rows) < _FISH_PAGE_SIZE):
            return resources
    raise ResourceListFailed(502, {
        "error": "provider_resource_page_limit",
        "message": "Fish Audio voice listing exceeded treg's 10,000-item safety limit",
    })


async def list_for_caller(
    caller: Caller,
    *,
    provider: str,
    kind: str,
    include_deleted: bool,
    source: str = "auto",
    upstream_client: httpx.AsyncClient,
    client_ip: str = "",
    client_name: str = "",
) -> ResourceList:
    """Use a Fish BYOK account when present; otherwise read the team's durable resources."""
    provider = provider.strip().lower()
    kind = kind.strip().lower()
    if source != "platform" and provider == "fishaudio" and kind == "voice" and not include_deleted:
        async with session_maker() as db:
            access = await catalog_endpoint_access(
                endpoint_id="fishaudio.voices.list",
                authorization_method="",
                caller=caller,
                db=db,
            )
        if access.get("tier") in ("tool", "credential"):
            resources = await _fish_account_voices(
                caller,
                upstream_client,
                client_ip=client_ip,
                client_name=client_name,
            )
            return ResourceList(source="byok", resources=resources)

    async with session_maker() as db:
        rows = await provider_resources.list_for_org(
            db,
            caller.org_id,
            provider=provider,
            resource_kind=kind,
            include_deleted=include_deleted,
        )
    return ResourceList(
        source="platform",
        resources=[provider_resources.view(row) for row in rows],
    )
