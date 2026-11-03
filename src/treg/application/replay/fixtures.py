"""A fixture corpus from this checkout's catalog: committed examples plus synthetic variants.

Every priced endpoint gets its committed example answer, an answer its adapter judges a miss, its
declared miss status, a 2xx error envelope, a 400 and a 500; an async endpoint also gets terminal
success and failure documents. Requests come from `test_request`. Nothing here is customer data.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator

from ...domain.catalog import store as catalog_store
from ...domain.catalog.routing import contracts as routing_contracts
from ...domain.catalog.routing import paths as routing_paths
from . import records

ERROR_2XX = {"error": {"code": "synthetic_error", "message": "synthetic failure"}}
ERROR_400 = {"error": "invalid request"}
ERROR_500 = {"error": "internal error"}

_STRING = re.compile(r"'[^']*'|\"[^\"]*\"")
_PATH = re.compile(
    r"(?<![\w.\]])(\.|[A-Za-z_]\w*(?:\[\d+\])?(?:\.[A-Za-z_]\w*(?:\[\d+\])?)*)(?!\s*\()")
_KEYWORDS = {"null", "true", "false", "and", "or", "not", "in", "None", "True", "False"}
_INDEXED = re.compile(r"(\w+)?\[(\d+)\]")
_DELETE = object()
_EQUALS = re.compile(r"^\s*([\w.]+)\s*==\s*(?:'([^']*)'|\"([^\"]*)\"|(\d+))\s*$")


def priced(cat: catalog_store.Catalog, ep: dict) -> bool:
    """A row that can hold money on treg's key: a positive price, a request table or a meter."""
    if ep.get("routed_children"):
        return False
    cost = ep.get("cost") or {}
    if cost.get("type") in (None, "free"):
        return False
    view = cat.cost_view(cost, ep["provider"]) or {}
    return bool((view.get("usd") or 0) > 0 or cost.get("table") or cost.get("usage"))


def _text(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _request(ep: dict) -> dict:
    sample = ep.get("test_request") or {}
    items: list[list[str]] = []
    for source in (sample.get("queryParams") or {}, sample.get("pathParams") or {}):
        for name, value in source.items():
            for one in value if isinstance(value, list) else [value]:
                items.append([str(name), _text(one)])
    body = sample.get("body")
    raw = b"" if body is None else body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return {"method": ep.get("method") or "GET", "vendor_url": None, "query_items": items,
            "path_params": {k: _text(v) for k, v in (sample.get("pathParams") or {}).items()},
            "body_b64": records.encode(raw), "accept": {}, "fidelity": "reconstructed"}


def _example(name: str | None) -> bytes | None:
    if not name:
        return None
    return (catalog_store.CATALOG_DIR / catalog_store.EXAMPLES_DIRNAME / name).read_bytes()


def _parsed(raw: bytes | None) -> object:
    try:
        return json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def _miss_paths(expression: str) -> list[str]:
    out: list[str] = []
    for match in _PATH.finditer(_STRING.sub("''", expression)):
        path = match.group(1)
        if path not in _KEYWORDS and path.split(".")[0] not in _KEYWORDS and path not in out:
            out.append(path)
    return out


def _set_adapter_path(doc: object, path: str, value: object) -> object:
    """Set (or with `_DELETE`, remove) `a.b[0].c` in place; a path that does not exist is left."""
    if path in (".", ""):
        return value
    parts = path.split(".")
    current = doc
    for n, part in enumerate(parts):
        last = n == len(parts) - 1
        indexed = _INDEXED.fullmatch(part)
        if indexed:
            name, index = indexed.group(1), int(indexed.group(2))
            if name:
                current = current.get(name) if isinstance(current, dict) else None
            if not isinstance(current, list) or index >= len(current):
                return doc
            if last:
                if value is _DELETE:
                    del current[index]
                else:
                    current[index] = value
                return doc
            current = current[index]
            continue
        if not isinstance(current, dict):
            return doc
        if last:
            if value is _DELETE:
                current.pop(part, None)
            else:
                current[part] = value
            return doc
        current = current.setdefault(part, {})
    return doc


def _empty_like(value: object) -> object:
    for kind, empty in ((bool, False), (list, []), (dict, {})):
        if isinstance(value, kind):
            return empty
    return None


def synthetic_miss(adapter, example: object) -> object | None:
    """The smallest blanking of the example along the adapter's own miss paths that the adapter
    judges a miss, else a bare document it judges one, else None."""
    paths = _miss_paths(adapter.miss)
    tries: list[list[tuple[str, object]]] = []
    if example is not None:
        for path in paths:
            for value in ([], None, False, "", 0, {}, _DELETE):
                tries.append([(path, value)])
        tries.append([(path, "empty_like") for path in paths])
        tries.append([(path, None) for path in paths])
    for edits in tries:
        doc = copy.deepcopy(example)
        for path, value in edits:
            if value == "empty_like":
                value = _empty_like(routing_paths.get_path(doc, path))
            doc = _set_adapter_path(doc, path, value)
        if _judged_miss(adapter, doc):
            return doc
    return next((doc for doc in ({}, [], {"data": None}) if _judged_miss(adapter, doc)), None)


def _judged_miss(adapter, doc: object) -> bool:
    try:
        return bool(adapter.is_miss(doc))
    except Exception:  # noqa: BLE001 - a document the predicate cannot read is no miss
        return False


def _declared_miss_body(miss: dict) -> bytes:
    found = _EQUALS.match(str(miss.get("when") or ""))
    if not found:
        return b"{}"
    path, single, double, number = found.groups()
    value = int(number) if number is not None else single if single is not None else double
    return json.dumps(_set_adapter_path({}, path, value)).encode()


def _with_status(document: object, dotted: str, word: object) -> object:
    """A copy of a terminal document with its status path set to `word` (async `a.0.b` paths)."""
    doc = copy.deepcopy(document) if isinstance(document, (dict, list)) else {}
    current = doc
    parts = dotted.split(".")
    for n, part in enumerate(parts):
        last = n == len(parts) - 1
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            if last:
                current[int(part)] = word
            else:
                current = current[int(part)]
        elif isinstance(current, dict):
            if last:
                current[part] = word
            else:
                current = current.setdefault(part, {})
        else:
            return None
    return doc


def _record(ep: dict, name: str, cls: str, status: int, body: bytes, request: dict) -> dict:
    return {
        "rid": f"fixture:{ep['id']}:{name}", "endpoint_id": ep["id"], "class": cls,
        "captured_at": None, "source": "fixture", "request": request,
        "response": {"status": status, "media_type": "application/json", "headers": None,
                     "body_b64": records.encode(body), "fidelity": "exact"},
        "recorded": {"estimate_micro": None, "observed_micro": None, "charged_micro": None,
                     "hit": None, "verdict": None, "credential_tier": "platform",
                     "routed_child": False},
        "async": None,
    }


def generate() -> Iterator[dict]:
    """Every fixture record for this checkout's catalog, in a stable order."""
    cat = catalog_store.load()
    for endpoint_id in sorted(cat.by_id):
        ep = cat.by_id[endpoint_id]
        if not priced(cat, ep):
            continue
        request = _request(ep)
        adapter = cat.adapters.get(endpoint_id)
        descriptor = ep.get("async") if isinstance(ep.get("async"), dict) else None
        example = _example(ep.get("example_file"))
        if example is not None:
            yield _record(ep, "example", "hit", 200, example, request)
        answer = _parsed(_example(ep.get("terminal_example_file")) if descriptor else example)
        if adapter is not None and adapter.miss.strip():
            missed = synthetic_miss(adapter, answer)
            if missed is not None:
                cls = "async_terminal" if descriptor else "miss"
                yield _record(ep, "miss", cls, 200, json.dumps(missed).encode(), request)
        miss = ep.get("miss") if isinstance(ep.get("miss"), dict) else {}
        status = routing_contracts.miss_status(ep)
        if status is not None:
            yield _record(ep, "declared_miss", "non2xx", status, _declared_miss_body(miss), request)
        elif miss.get("status") == 204:
            yield _record(ep, "declared_miss", "miss", 204, b"", request)
        yield _record(ep, "error_2xx", "miss", 200, json.dumps(ERROR_2XX).encode(), request)
        yield _record(ep, "error_400", "non2xx", 400, json.dumps(ERROR_400).encode(), request)
        yield _record(ep, "error_500", "non2xx", 500, json.dumps(ERROR_500).encode(), request)
        if descriptor is None:
            continue
        rule = descriptor.get("status") or {}
        if answer is not None:
            yield _record(ep, "terminal", "async_terminal", 200, json.dumps(answer).encode(), request)
        for outcome in ("failure", "billed_failure"):
            words = rule.get(outcome) or []
            doc = _with_status(answer, str(rule.get("path") or ""), words[0]) if words else None
            if doc is not None:
                yield _record(ep, f"terminal_{outcome}", "async_terminal", 200,
                              json.dumps(doc).encode(), request)
