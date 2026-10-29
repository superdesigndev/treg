"""Export recorded production exchanges as a replay corpus (docs/context/architecture/replay.md).

The one part of the replay package that reads a database and the archive; the engine never does.
It reads what production already retains through the readers the Arena collector uses: the call
row, the exact archived answer it received (R2 first, the database as fallback), the redacted
failure evidence, and the terminal answer of an async task. It writes two private files and prints
counts only: a body never reaches a log line.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import zlib
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.exc import DBAPIError

from ... import archive, archive_bodies, oauth_providers
from ...domain import asynctasks as asynctasks_rules
from ...domain.catalog import store as catalog_store
from ...infra.db import _canceled_by_timeout, session_maker
from ...models import AsyncTaskRecord, CallRecord, Org, Tool
from ...timeutil import utcnow_naive
from .. import evidence_retention
from ..arena_insights import _evidence_pointers
from ..call import evidence
from ..call.settle import _CREDIT_HEADERS
from . import fixtures, records

PER_CLASS = 20
PER_CLASS_MAX = 100
DAYS = 30
DAYS_MAX = 90
STATEMENT_TIMEOUT_S = 20
SYNC_CLASSES = ("hit", "miss", "non2xx")
_SCAN_FACTOR = 5      # candidates read per class, so repeats and drops still leave N
_NARROWINGS = 3       # a class query that times out is retried on half its window, this often
_READS = 8            # simultaneous body reads, as the Arena reader
_ROUTED_CHILD = re.compile(r":r\d+$")
_PLACEHOLDER = re.compile(r"{(\w+)}")
_NO_EVIDENCE = "<no response body or headers>"
_BODYLESS = {"GET", "HEAD", "DELETE"}
_EVIDENCE_HEADER = re.compile(
    r"(?:^|\s)(" + "|".join(map(re.escape, evidence._EVIDENCE_HEADERS)) + r")=")
_COLUMNS = (CallRecord.id, CallRecord.created_at, CallRecord.method, CallRecord.status_code,
            CallRecord.path, CallRecord.call_ref, CallRecord.params_hash,
            CallRecord.archive_key_hash, CallRecord.archive_content_hash,
            CallRecord.cost_estimated_micro, CallRecord.cost_observed_micro,
            CallRecord.cost_charged_micro, CallRecord.hit, CallRecord.verdict)
_EVIDENCE_COLUMNS = (CallRecord.error_request, CallRecord.error_response)


@dataclass
class _Tally:
    candidates: int = 0
    exported: int = 0
    questions: set = field(default_factory=set)
    dropped: Counter = field(default_factory=Counter)
    window_days: float | None = None

    def view(self) -> dict:
        out = {"candidates": self.candidates, "exported": self.exported,
               "distinct": len(self.questions), "dropped": dict(sorted(self.dropped.items()))}
        if self.window_days is None:
            out["timed_out"] = True
        else:
            out["window_days"] = round(self.window_days, 3)
        return out


@dataclass
class _Run:
    until: datetime
    days: float
    per_class: int
    statement_timeout_s: float
    session_factory: Callable
    corpus: TextIO
    manifest: TextIO
    records: int = 0

    def write(self, record: dict, *, callrecord_id: int | None, async_call_id: str | None = None):
        self.corpus.write(records.line(record) + "\n")
        self.manifest.write(records.line({"rid": record["rid"], "callrecord_id": callrecord_id,
                                          "async_call_id": async_call_id}) + "\n")
        self.records += 1


def select_endpoints(endpoints: Iterable[str] = (), providers: Iterable[str] = ()) -> list[str]:
    """The named endpoints plus every priced endpoint of the named providers, from this checkout's
    catalog. An unknown name is an error, never an empty export."""
    cat = catalog_store.load()
    wanted = {e for e in endpoints if e}
    names = {p for p in providers if p}
    unknown = sorted(wanted - set(cat.by_id))
    if unknown:
        raise ValueError(f"not in this checkout's catalog: {', '.join(unknown)}")
    by_provider = {eid for eid, ep in cat.by_id.items()
                   if ep["provider"] in names and fixtures.priced(cat, ep)}
    missing = sorted(names - {cat.by_id[eid]["provider"] for eid in by_provider})
    if missing:
        raise ValueError(f"no priced endpoint for: {', '.join(missing)}")
    return sorted(wanted | by_provider)


async def export(endpoint_ids: Iterable[str], out: Path, manifest: Path, *,
                 per_class: int = PER_CLASS, days: float = DAYS,
                 statement_timeout_s: float = STATEMENT_TIMEOUT_S,
                 session_factory=session_maker) -> dict:
    """Write up to `per_class` records per endpoint and class to `out`, and each record's call row
    to `manifest`. Both files are created (never overwritten) readable by their owner only; a run
    that fails removes them. Returns the counts."""
    if not 1 <= per_class <= PER_CLASS_MAX or not 0 < days <= DAYS_MAX:
        raise ValueError(f"per_class is 1..{PER_CLASS_MAX} and days is up to {DAYS_MAX}")
    if Path(out).resolve() == Path(manifest).resolve():
        raise ValueError("the corpus and the manifest are two files")
    cat = catalog_store.load()
    until = utcnow_naive()
    summary: dict = {"until": until.isoformat() + "Z", "days": days, "per_class": per_class,
                     "endpoints": {}}
    created: list[Path] = []
    try:
        with _private(out, created) as corpus, _private(manifest, created) as index:
            run = _Run(until, days, per_class, statement_timeout_s, session_factory, corpus, index)
            for endpoint_id in endpoint_ids:
                summary["endpoints"][endpoint_id] = await _endpoint(cat.by_id[endpoint_id], run)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    summary["records"] = run.records
    return summary


class _private:
    """A new file only its owner can read, recorded in `created` so a failed run removes it."""

    def __init__(self, path: Path, created: list[Path]):
        self.path, self.created = Path(path), created

    def __enter__(self) -> TextIO:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.created.append(self.path)
        self.file = os.fdopen(fd, "w", encoding="utf-8")
        return self.file

    def __exit__(self, *exc) -> None:
        self.file.close()


async def _endpoint(ep: dict, run: _Run) -> dict:
    renderings = _renderings(ep["provider"])
    if renderings is None:
        # The credential scan cannot run, so nothing of this endpoint can be shown clean.
        return {"error": "credential_renderings_unavailable"}
    out = {cls: (await _sync_class(ep, cls, renderings, run)).view() for cls in SYNC_CLASSES}
    if isinstance(ep.get("async"), dict):
        out["async_terminal"] = (await _async_class(ep, renderings, run)).view()
    return out


# ---- reading ----------------------------------------------------------------------------------

def _team_in_archive(org_id):
    """The team still exists and has not opted out of the archive (erasure requires the opt-out)."""
    return exists().where(Org.id == org_id, Org.archive_opt_out_at.is_(None))


def _class_filter(cls: str) -> tuple:
    charged = CallRecord.cost_charged_micro
    answered = CallRecord.status_code.between(200, 299)
    if cls == "hit":
        return answered, or_(CallRecord.hit.is_(True), and_(CallRecord.hit.is_(None), charged > 0))
    if cls == "miss":
        return answered, or_(CallRecord.hit.is_(False), and_(CallRecord.hit.is_(None), charged == 0))
    return (or_(CallRecord.status_code >= 400, CallRecord.status_code < 200),
            CallRecord.upstream_ms.is_not(None), CallRecord.error_response.is_not(None),
            CallRecord.error_response.not_like(evidence_retention.ERROR_EVIDENCE_EXPIRED + "%"),
            CallRecord.error_response.not_like("<redacted:%"),
            or_(CallRecord.error_request.is_(None),
                CallRecord.error_request != evidence._ERROR_MASKING_FAILED))


async def _bounded(run: _Run, build: Callable) -> tuple[list, timedelta | None]:
    """The rows `build(since)` selects, newest first, under a statement timeout; a query that
    times out is retried on half its window. -> (rows, the window read, None if none finished)"""
    window = timedelta(days=run.days)
    for _ in range(_NARROWINGS + 1):
        try:
            async with run.session_factory() as db:
                if db.get_bind().dialect.name == "postgresql":
                    await db.execute(text(
                        f"SET LOCAL statement_timeout = {int(run.statement_timeout_s * 1000)}"))
                return list((await db.execute(build(run.until - window))).all()), window
        except DBAPIError as exc:
            if not _canceled_by_timeout(exc):
                raise
        window /= 2
    return [], None


def _distinct_first(rows: list, identity: Callable) -> list:
    """One row per question, newest first, then the repeats, newest first."""
    first, repeats, seen = [], [], set()
    for row in rows:
        key = identity(row)
        (repeats if key is not None and key in seen else first).append(row)
        seen.add(key)
    return first + repeats


async def _take(candidates: list, run: _Run, tally: _Tally, fetch: Callable[[list], Awaitable],
                build: Callable) -> list[tuple]:
    """Accept up to `run.per_class` candidates in order: fetch a chunk's evidence (in short
    sessions, the reads after they close), build each record or a reason to drop it, and read
    the next chunk until enough are accepted."""
    accepted: list[tuple] = []
    start = 0
    while len(accepted) < run.per_class and start < len(candidates):
        chunk = candidates[start:start + max(run.per_class - len(accepted), _READS)]
        start += len(chunk)
        found = await fetch(chunk)
        for row in chunk:
            if len(accepted) == run.per_class:
                break
            record, why = build(row, found)
            if record is None:
                tally.dropped[why] += 1
            else:
                accepted.append((row, record))
    return accepted


async def _answers(ep: dict, rows: list, run: _Run) -> dict[int, tuple]:
    """row id -> (archive key, media type, exact answer bytes) for each row whose answer is kept."""
    async with run.session_factory() as db:
        keymap, pointers, media = await _evidence_pointers(db, rows, path="replay")
    semaphore = asyncio.Semaphore(_READS)

    async def load(pair, pointer):
        async with semaphore:
            try:
                return pair, await archive_bodies.read(pointer, "replay",
                                                       session_factory=run.session_factory)
            except zlib.error:
                return pair, None

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(load(pair, pointer)) for pair, pointer in pointers.items()]
    bodies = {pair: body for pair, body in (task.result() for task in tasks) if body is not None}
    out = {}
    for row in rows:
        key = keymap.get(row.archive_key_hash)
        if key is None or key.endpoint_id != ep["id"] or not key.req_method:
            continue
        body = bodies.get((key.id, row.archive_content_hash))
        if body is not None:
            out[row.id] = (key, media.get((key.id, row.archive_content_hash)) or "", body)
    return out


async def _sync_class(ep: dict, cls: str, renderings: list[str], run: _Run) -> _Tally:
    tally = _Tally()
    columns = _COLUMNS + (_EVIDENCE_COLUMNS if cls == "non2xx" else ())

    def build_query(since):
        if cls == "non2xx":
            # Evidence past its retention is never read, whether or not the purge has run yet.
            since = max(since, run.until - timedelta(days=evidence_retention.ERROR_EVIDENCE_TTL_DAYS))
        return (select(*columns).where(
            CallRecord.endpoint_id == ep["id"], CallRecord.created_at >= since,
            CallRecord.created_at < run.until, CallRecord.credential_tier == "platform",
            CallRecord.kind == "call", CallRecord.cached.is_(False),
            CallRecord.refused_by.is_(None), CallRecord.cost_charged_micro.is_not(None),
            _team_in_archive(CallRecord.org_id), *_class_filter(cls))
            .order_by(CallRecord.created_at.desc()).limit(run.per_class * _SCAN_FACTOR))

    rows, window = await _bounded(run, build_query)
    tally.candidates = len(rows)
    tally.window_days = window / timedelta(days=1) if window is not None else None
    if cls == "non2xx":
        async def no_fetch(chunk):
            return {}
        accepted = await _take(_distinct_first(rows, lambda r: r.params_hash), run, tally,
                               no_fetch, lambda row, _: _failure_record(ep, row, renderings))
        identity = "params_hash"
    else:
        kept = []
        for row in rows:
            if row.archive_key_hash and row.archive_content_hash:
                kept.append(row)
            else:
                # Never recorded: an answer the endpoint's `expect` rule failed, a forbidden
                # policy, a submission or a spooled body. Counted, never invented.
                tally.dropped["expect_unavailable" if ep.get("expect") else "not_archived"] += 1

        async def fetch(chunk):
            return await _answers(ep, chunk, run)

        def build(row, found):
            if row.id not in found:
                return None, ("policy_forbidden" if not archive.storable(ep) else "no_body")
            return _answer_record(ep, cls, row, *found[row.id], renderings)

        accepted = await _take(_distinct_first(kept, lambda r: r.archive_key_hash), run, tally,
                               fetch, build)
        identity = "archive_key_hash"
    for row, record in accepted:
        run.write(record, callrecord_id=row.id)
        tally.questions.add(getattr(row, identity))
    tally.exported = len(accepted)
    return tally


async def _async_class(ep: dict, renderings: list[str], run: _Run) -> _Tally:
    tally = _Tally()

    def build_query(since):
        return (select(AsyncTaskRecord).where(
            AsyncTaskRecord.endpoint_id == ep["id"],
            AsyncTaskRecord.status.in_((asynctasks_rules.SETTLED, asynctasks_rules.RELEASED)),
            AsyncTaskRecord.created_at >= since, AsyncTaskRecord.created_at < run.until,
            _team_in_archive(AsyncTaskRecord.org_id))
            .order_by(AsyncTaskRecord.created_at.desc()).limit(run.per_class * _SCAN_FACTOR))

    rows, window = await _bounded(run, build_query)
    tasks = [row for (row,) in rows]
    tally.candidates = len(tasks)
    tally.window_days = window / timedelta(days=1) if window is not None else None
    identity = lambda task: (task.status, task.hit, task.verdict)  # noqa: E731

    async def fetch(chunk):
        return await archive.load_terminal_snapshots(
            [(task.call_id, task.endpoint_id) for task in chunk],
            session_factory=run.session_factory)

    def build(task, found):
        if task.call_id not in found:
            return None, "no_terminal_evidence"
        return _terminal_record(ep, task, *found[task.call_id], renderings)

    accepted = await _take(_distinct_first(tasks, identity), run, tally, fetch, build)
    calls = {}
    if accepted:
        async with run.session_factory() as db:
            calls = dict((await db.execute(select(CallRecord.call_ref, CallRecord.id).where(
                CallRecord.call_ref.in_([task.call_id for task, _ in accepted])))).all())
    for task, record in accepted:
        run.write(record, callrecord_id=calls.get(task.call_id), async_call_id=task.call_id)
        tally.questions.add(identity(task))
    tally.exported = len(accepted)
    return tally


# ---- the records ------------------------------------------------------------------------------

def _renderings(provider: str) -> list[str] | None:
    """Every spelling of every value of treg's own credential for `provider` (all its accounts),
    lowercased; None when they cannot be rendered or none is configured."""
    profile = oauth_providers.get(provider)
    if profile is None:
        return None
    tool = Tool(name=provider, base_url=profile.base_url,
                bindings=oauth_providers.platform_bindings(profile))
    found = evidence._safe_secret_renderings(tool, {})
    return [s.lower() for s in found] if found else None


def _echoes(renderings: list[str], *parts: bytes | str | None) -> bool:
    """Whether any part carries a rendering, as is or percent-decoded and JSON-unescaped: the
    probe the caller-facing error masking uses."""
    for part in parts:
        if not part:
            continue
        raw = part.decode("utf-8", "surrogateescape") if isinstance(part, bytes) else part
        plain = raw.lower()
        probe = unquote(raw.replace("\\/", "/")).lower()
        if any(s in plain or s in probe for s in renderings):
            return True
    return False


def _template(ep: dict) -> tuple[re.Pattern, list[tuple[str, str]]]:
    path, _, fixed = (ep.get("path") or "/").partition("?")
    pieces = _PLACEHOLDER.split("/" + path.lstrip("/"))
    pattern = "".join(re.escape(piece) if n % 2 == 0 else f"(?P<{piece}>[^/]+)"
                      for n, piece in enumerate(pieces))
    return re.compile(".*" + pattern), parse_qsl(fixed, keep_blank_values=True)


def _path_params(ep: dict, url: str) -> dict[str, str] | None:
    """The `{placeholder}` values the resolver rendered into `url`'s path, or None when the path
    does not match the endpoint's template."""
    pattern, _ = _template(ep)
    match = pattern.fullmatch(urlsplit(url).path)
    if match is None:
        return None
    return {name: unquote(value) for name, value in match.groupdict().items()}


def _caller_request(ep: dict, url: str) -> tuple[list[list[str]], dict[str, str], bool] | None:
    """The caller's query as pricing reads it (path parameters included), the path parameters,
    and whether either had to be reconstructed, from the vendor-facing URL the archive keeps."""
    params = _path_params(ep, url)
    if params is None:
        return None
    _, fixed = _template(ep)
    items = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    for pair in fixed:
        if pair in items:
            items.remove(pair)
    return ([[k, v] for k, v in items] + [[k, v] for k, v in params.items()], params,
            bool(params or fixed))


def _credit_header(ep: dict, observed_micro: int | None) -> dict[str, str] | None:
    """The credit header a header-billed provider's settle reads, synthesized from the charge
    production observed: the exact inverse of the settle's conversion."""
    spec = _CREDIT_HEADERS.get(ep["provider"])
    rate = catalog_store.load().credit_rates.get(ep["provider"])
    if spec is None or observed_micro is None or not rate:
        return None
    name, multiplier = spec
    return {name: repr(observed_micro / 1_000_000 / float(rate) * multiplier)}


def _recorded(row) -> dict:
    return {"estimate_micro": row.cost_estimated_micro, "observed_micro": row.cost_observed_micro,
            "charged_micro": row.cost_charged_micro, "hit": row.hit, "verdict": row.verdict,
            "credential_tier": "platform",
            "routed_child": bool(_ROUTED_CHILD.search(row.call_ref or ""))}


def _record(ep: dict, cls: str, source: str, at: datetime, request: dict, response: dict,
            recorded: dict, asynchronous: dict | None = None) -> dict:
    return {"rid": f"prod:{secrets.token_hex(8)}", "endpoint_id": ep["id"], "class": cls,
            "captured_at": at.date().isoformat(), "source": source, "request": request,
            "response": response, "recorded": recorded, "async": asynchronous}


def _with_headers(response: dict, headers: dict | None) -> dict:
    if headers:
        response["headers"] = {**(response.get("headers") or {}), **headers}
        response["headers_fidelity"] = "synthesized"
    return response


def _answer_record(ep: dict, cls: str, row, key, media_type: str, body: bytes,
                   renderings: list[str]) -> tuple[dict | None, str | None]:
    caller = _caller_request(ep, key.req_url)
    if caller is None:
        return None, "path_unmatched"
    if _echoes(renderings, key.req_url, key.req_body, json.dumps(key.req_headers or {}), body):
        return None, "credential_echo"
    items, params, reconstructed = caller
    request = {"method": key.req_method, "vendor_url": key.req_url, "query_items": items,
               "path_params": params, "body_b64": records.encode(key.req_body or b""),
               "accept": dict(key.req_headers or {}),
               "fidelity": "reconstructed" if reconstructed else "exact"}
    response = _with_headers({"status": row.status_code, "media_type": media_type, "headers": None,
                              "body_b64": records.encode(body), "fidelity": "exact"},
                             _credit_header(ep, row.cost_observed_micro))
    return _record(ep, cls, "archive", row.created_at, request, response, _recorded(row)), None


def _snippet_request(snippet: str, method: str) -> tuple[list[list[str]], str]:
    """The caller's query items and body from failure evidence (`?k=v&k=v body`, redacted)."""
    query, body = "", snippet
    if snippet.startswith("?"):
        query, body = snippet[1:], ""
        if method.upper() not in _BODYLESS:
            starts = [n for n in (query.rfind(" {"), query.rfind(" [")) if n >= 0]
            if starts:
                query, body = query[:max(starts)], query[max(starts) + 1:]
    return [list(part.partition("=")[::2]) for part in query.split("&") if part], body


def _snippet_response(snippet: str) -> tuple[dict[str, str] | None, str]:
    """The allowlisted headers failure evidence inlines (`[name=value ...] `) and the body."""
    if snippet == _NO_EVIDENCE:
        return None, ""
    if snippet.startswith("[") and "] " in snippet:
        inner, body = snippet[1:].split("] ", 1)
        marks = list(_EVIDENCE_HEADER.finditer(inner))
        if marks and marks[0].start() == 0:
            ends = [m.start() for m in marks[1:]] + [len(inner)]
            return {m.group(1): inner[m.end():end].strip() for m, end in zip(marks, ends)}, body
    return None, snippet


def _failure_record(ep: dict, row, renderings: list[str]) -> tuple[dict | None, str | None]:
    params = _path_params(ep, row.path or "")
    if params is None:
        return None, "path_unmatched"
    if _echoes(renderings, row.path, row.error_request, row.error_response):
        return None, "credential_echo"
    items, body = _snippet_request(row.error_request or "", row.method or "")
    headers, answer = _snippet_response(row.error_response or "")
    named = {k for k, _ in items}
    request = {"method": row.method, "vendor_url": row.path,
               "query_items": items + [[k, v] for k, v in params.items() if k not in named],
               "path_params": params,
               "body_b64": records.encode(body.encode()), "accept": {}, "fidelity": "snippet"}
    response = _with_headers({"status": row.status_code, "media_type": "", "headers": headers,
                              "body_b64": records.encode(answer.encode()),
                              "fidelity": "snippet_redacted_truncated"},
                             _credit_header(ep, row.cost_observed_micro))
    return (_record(ep, "non2xx", "error_evidence", row.created_at, request, response,
                    _recorded(row)), None)


def _terminal_record(ep: dict, task: AsyncTaskRecord, status: int, body: bytes,
                     renderings: list[str]) -> tuple[dict | None, str | None]:
    basis = task.settlement_basis or {}
    if _echoes(renderings, json.dumps(basis), json.dumps(task.descriptor or {}), body):
        return None, "credential_echo"
    # Only a table basis keeps the request it priced; without one the reserve is unavailable.
    asked = (basis.get("amount") or {}).get("request")
    if isinstance(asked, dict):
        query = asked.get("queryParams") or {}
        params = {k: str(v) for k, v in (asked.get("pathParams") or {}).items()}
        items = [[k, str(one)] for k, v in query.items()
                 for one in (v if isinstance(v, list) else [v])]
        raw = json.dumps(asked["body"]).encode() if asked.get("body") else b""
        request = {"method": ep.get("method") or "GET", "vendor_url": None,
                   "query_items": items + [[k, v] for k, v in params.items()],
                   "path_params": params, "body_b64": records.encode(raw), "accept": {},
                   "fidelity": "reconstructed"}
    else:
        request = {"method": ep.get("method") or "GET", "vendor_url": None, "query_items": [],
                   "path_params": {}, "body_b64": "", "accept": {}, "fidelity": "unavailable"}
    response = {"status": status, "media_type": "application/json", "headers": None,
                "body_b64": records.encode(body), "fidelity": "exact"}
    recorded = {"estimate_micro": basis.get("reserve_micro"), "observed_micro": None,
                "charged_micro": task.settled_micro, "hit": task.hit, "verdict": task.verdict,
                "credential_tier": "platform", "routed_child": False}
    asynchronous = {"descriptor": task.descriptor, "settlement_basis": basis,
                    "status": task.status, "settled_micro": task.settled_micro}
    return (_record(ep, "async_terminal", "async_terminal", task.created_at, request, response,
                    recorded, asynchronous), None)
