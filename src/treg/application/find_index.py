"""Card vectors for find's semantic channel: computed per catalog, cached by card, never committed.

A card (`find_recall.Unit.text`) is embedded once per model. Its vector is stored in the archive's
object store under `find-vectors/<model slug>/<sha256 of the card>`; the first use of a catalog
reads the vectors it already has, embeds only the cards it does not (in batches, one API request per
batch), writes those back, and assembles one float32 matrix aligned with the index's units. Until
that is done the semantic channel is off and find answers from the lexical channel alone: the page
never waits for vectors.

No object store (self-hosted, or archive R2 off): vectors are computed in this process only, so each
start pays for them again. No embedding API either: the channel stays off. Several instances each
build their own matrix from the same cache; two starting on the same new cards both embed them,
which is cheap enough to need no lock. A new model is a new prefix, so vectors of different models
never meet.

Never holds a database connection: this is object-store and HTTP I/O only.

`warm` starts the build at startup (a background task of the lifespan, cancelled with it) so the
first find after a deploy does not pay for it; otherwise the first find on a catalog starts it.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import struct
import time
from dataclasses import dataclass

from ..config import get_settings
from ..domain.catalog import find_recall
from ..domain.catalog import store as catalog_store
from ..infra import embed as embed_infra
from ..infra.object_store import NamedObjectStore

log = logging.getLogger("treg.find_index")

MAGIC = b"TRV1"               # a stored vector: MAGIC, uint32 dim, 32-byte card hash, dim float32
BATCH = 96                    # cards per embedding request
BUILD_TIMEOUT_S = 30.0        # per embedding request while building (not the query's timeout)
READ_CONCURRENCY = 32
BATCH_RETRIES = 2             # a failed embedding batch is retried this often before the build fails
RETRY_BACKOFF_S = 1.0         # ...after this long, doubling
RETRY_AFTER_S = 300.0         # a failed build is retried by the next find after this long

_store: NamedObjectStore | None = None     # set at startup


def configure(store: NamedObjectStore | None) -> None:
    """Composition seam: bootstrap hands in the archive's object store (or None)."""
    global _store
    _store = store


@dataclass
class Vectors:
    model: str
    matrix: object            # numpy float32 (units x dim), rows L2-normalised; zero row = no vector
    dim: int
    reused: int               # vectors read from the object store
    computed: int             # vectors embedded by this build


@dataclass
class _Build:
    cat: catalog_store.Catalog
    task: asyncio.Task | None
    vectors: Vectors | None = None
    failed_at: float | None = None
    error: str | None = None


_build: _Build | None = None


def api_key() -> str:
    """The embedding key: its own setting, else treg's OpenRouter key when the URL is OpenRouter's."""
    s = get_settings()
    if s.find_embed_api_key:
        return s.find_embed_api_key
    return s.platform_key_openrouter if "openrouter.ai" in s.find_embed_url else ""


def enabled() -> bool:
    return bool(api_key())


def model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", model.lower()).strip("-.")[:128] or "model"


def card_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _name(model: str, key: str) -> str:
    return f"find-vectors/{model_slug(model)}/{key}"


def encode(key: str, vector: list[float]) -> bytes:
    return MAGIC + struct.pack("<I", len(vector)) + bytes.fromhex(key) + struct.pack(f"<{len(vector)}f", *vector)


def decode(key: str, body: bytes | None):
    """A stored vector as a float32 array, or None when it is missing, damaged, or another card's."""
    import numpy as np

    if not body or len(body) < 40 or body[:4] != MAGIC:
        return None
    (dim,) = struct.unpack("<I", body[4:8])
    if body[8:40] != bytes.fromhex(key) or len(body) != 40 + 4 * dim or dim == 0:
        return None
    return np.frombuffer(body, dtype="<f4", offset=40)


async def _read(model: str, keys: list[str]) -> dict:
    if _store is None:
        return {}
    sem = asyncio.Semaphore(READ_CONCURRENCY)
    found: dict = {}

    async def one(key: str) -> None:
        async with sem:
            try:
                v = decode(key, await _store.get_named(_name(model, key)))
            except Exception:  # noqa: BLE001 - an unreadable vector is recomputed, not fatal
                return
            if v is not None:
                found[key] = v
    await asyncio.gather(*(one(k) for k in set(keys)))
    return found


async def _write(model: str, vectors: dict[str, list[float]]) -> None:
    if _store is None or not vectors:
        return
    sem = asyncio.Semaphore(READ_CONCURRENCY)

    async def one(key: str, v: list[float]) -> None:
        async with sem:
            try:
                await _store.put_named(_name(model, key), encode(key, v))
            except Exception:  # noqa: BLE001 - an unwritten vector is recomputed by the next start
                log.warning("find vector not cached (%s)", key[:12])
    await asyncio.gather(*(one(k, v) for k, v in vectors.items()))


async def build(ix: find_recall.Index, *, transport=None) -> Vectors:
    """The matrix for `ix`: vectors read from the cache, the rest embedded and written back. Raises
    RuntimeError when the API cannot supply a missing vector (the caller keeps the channel off)."""
    import numpy as np

    s = get_settings()
    model = s.find_embed_model
    keys = [card_key(u.text) for u in ix.units]
    text_of = {k: u.text for k, u in zip(keys, ix.units)}
    have = await _read(model, keys)
    dims = {len(v) for v in have.values()}
    if len(dims) > 1:   # the model changed shape under one name: trust nothing cached
        have, dims = {}, set()
    dim = dims.pop() if dims else None
    missing = sorted({k for k in keys if k not in have})
    made: dict = {}
    for i in range(0, len(missing), BATCH):
        batch = missing[i:i + BATCH]
        for attempt in range(BATCH_RETRIES + 1):
            e = await embed_infra.embed([text_of[k] for k in batch], api_key=api_key(), model=model,
                                        url=s.find_embed_url, timeout_s=BUILD_TIMEOUT_S, dim=dim,
                                        transport=transport)
            if e.vectors is not None or not _transient(e.error) or attempt == BATCH_RETRIES:
                break
            await asyncio.sleep(RETRY_BACKOFF_S * 2 ** attempt)
        if e.vectors is None:
            await _write(model, made)   # keep what this build already paid for
            raise RuntimeError(e.error or "embedding failed")
        dim = dim or len(e.vectors[0])
        made.update(zip(batch, e.vectors))
    await _write(model, made)
    every = {**have, **made}
    matrix = np.zeros((len(keys), dim or 1), dtype=np.float32)
    for row, key in enumerate(keys):
        matrix[row] = every[key]
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= np.where(norms == 0, 1, norms)
    return Vectors(model=model, matrix=matrix, dim=dim or 0, reused=len(have), computed=len(made))


def _transient(error: str | None) -> bool:
    """Worth another try: a timeout, a 429 or 5xx, a dropped connection. Not a refused key (4xx) or a
    vector of the wrong size - asking again answers the same."""
    if not error:
        return True
    if error.startswith("dim_") or error == "bad_body":
        return False
    return not error.startswith("http_4") or error == "http_429"


def ready(cat: catalog_store.Catalog, ix: find_recall.Index) -> Vectors | None:
    """The vectors for this catalog if built; otherwise start (or, after a failure, retry) the build
    in the background and answer None for now."""
    global _build
    if not enabled():
        return None
    b = _build
    model = get_settings().find_embed_model
    if b is not None and b.cat is cat:
        if b.vectors is not None and b.vectors.model == model:
            return b.vectors
        if b.task is not None and not b.task.done():
            return None
        if b.failed_at is not None and time.monotonic() - b.failed_at < RETRY_AFTER_S:
            return None
    b = _Build(cat=cat, task=None)
    b.task = asyncio.create_task(_run(b, ix))
    _build = b
    return None


async def _run(b: _Build, ix: find_recall.Index, transport=None) -> None:
    """Build into `b`; a failure is recorded on it (and retried later by `ready`), never raised."""
    t0 = time.perf_counter()
    try:
        b.vectors = await build(ix, transport=transport)
        log.info("find vectors ready: %d reused, %d computed, %.1fs", b.vectors.reused, b.vectors.computed,
                 time.perf_counter() - t0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the lexical channel carries find until a retry succeeds
        b.failed_at, b.error = time.monotonic(), str(exc)[:80]
        log.warning("find vectors unavailable: %s", b.error)


async def warm() -> None:
    """Build this process's card vectors now rather than on its first find. The catalog's parse
    (seconds, normally already done at import) and the index's build (under a second) run off the
    event loop; both are cached on the one object the vectors are keyed by. Awaits the build so
    cancelling this task cancels it; a failed build is logged by `_run` and retried by the next
    find."""
    if not enabled():
        return
    cat = await asyncio.to_thread(catalog_store.load)
    ix = await asyncio.to_thread(find_recall.index, cat)
    ready(cat, ix)
    b = _build
    if b is not None and b.cat is cat and b.task is not None:
        await b.task


async def prepare(cat: catalog_store.Catalog, ix: find_recall.Index, *, transport=None) -> Vectors | None:
    """Build this catalog's vectors now and install them (the bench, which must not score a query
    before the channel is on). None when the channel is off or the build failed."""
    global _build
    if not enabled():
        return None
    _build = _Build(cat=cat, task=None)
    await _run(_build, ix, transport)
    return _build.vectors


@dataclass(frozen=True)
class Semantic:
    scores: list[float] | None     # one per unit; None = the channel is off for this query
    ms: int | None = None
    error: str | None = None       # off | not_ready | an embedding error


async def semantic(query: str, cat: catalog_store.Catalog, ix: find_recall.Index, *,
                   transport=None) -> Semantic:
    """The semantic channel for one query: the query's vector against every card. Never raises."""
    if not enabled():
        return Semantic(None, error="off")
    vectors = ready(cat, ix)
    if vectors is None:
        return Semantic(None, error="not_ready")
    import numpy as np

    s = get_settings()
    e = await embed_infra.embed_query(query, api_key=api_key(), model=vectors.model, url=s.find_embed_url,
                                      timeout_s=float(s.find_embed_timeout_s), dim=vectors.dim,
                                      transport=transport)
    if e.vector is None:
        return Semantic(None, ms=e.ms, error=e.error)
    q = np.asarray(e.vector, dtype=np.float32)
    n = float(np.linalg.norm(q))
    if n == 0:
        return Semantic(None, ms=e.ms, error="bad_body")
    return Semantic((vectors.matrix @ (q / n)).tolist(), ms=e.ms)


def reset() -> None:
    """Forget the current build (tests)."""
    global _build
    if _build is not None and _build.task is not None and not _build.task.done():
        _build.task.cancel()
    _build = None
