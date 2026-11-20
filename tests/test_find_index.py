"""Card vectors for find's semantic channel (application.find_index): built per catalog, cached per
card in the object store, recomputed only where missing, and off - never failing a find - while the
API or the store is down."""
from __future__ import annotations

import asyncio

import pytest

from tests.fake_object_store import MemoryObjectStore
from tests.test_find_recall import _cat
from treg.application import find_index
from treg.config import get_settings
from treg.domain.catalog import find_recall as fr
from treg.domain.catalog import store as catalog_store
from treg.infra import embed as embed_infra

TOPICS = (("image", "picture", "photo"), ("email", "mail"), ("comment",), ("people", "person"))


def _vector(text: str) -> list[float]:
    """A deterministic stand-in for a model: which of four topics a text mentions (a topic has
    synonyms, which is the point of the channel), plus a constant."""
    t = text.lower()
    return [1.0 if any(w in t for w in words) else 0.0 for words in TOPICS] + [0.1]


@pytest.fixture
def api(monkeypatch):
    """A fake embeddings API, counting the texts it is asked to embed; `down` makes it abstain."""
    s = get_settings()
    monkeypatch.setattr(s, "find_embed_api_key", "test-key", raising=False)
    monkeypatch.setattr(s, "find_embed_model", "test/model-1", raising=False)
    state = {"texts": [], "down": False}

    async def fake_embed(texts, **kw):
        if state["down"]:
            return embed_infra.Embedding(vectors=None, ms=5, error="http_503")
        state["texts"].extend(texts)
        return embed_infra.Embedding(vectors=[_vector(t) for t in texts], ms=5)
    monkeypatch.setattr(embed_infra, "embed", fake_embed)
    monkeypatch.setattr(find_index, "RETRY_BACKOFF_S", 0.0)
    embed_infra.clear_cache()
    find_index.reset()
    find_index.configure(None)
    yield state
    find_index.reset()
    find_index.configure(None)


async def test_a_build_embeds_every_card_once_and_caches_each_by_its_hash(api):
    ix = fr.build(_cat())
    store = MemoryObjectStore()
    find_index.configure(store)
    v = await find_index.build(ix)
    assert (v.computed, v.reused, v.dim) == (len(ix.units), 0, 5) and len(api["texts"]) == len(ix.units)
    assert len(store.objects) == len({find_index.card_key(u.text) for u in ix.units})
    assert all(k.startswith("find-vectors/test-model-1/") for k in store.objects)
    norms = (v.matrix ** 2).sum(axis=1)
    assert abs(norms.max() - 1) < 1e-5

    # the next start reads them all back and embeds nothing
    api["texts"].clear()
    again = await find_index.build(ix)
    assert (again.computed, again.reused, api["texts"]) == (0, v.computed, [])
    assert (again.matrix == v.matrix).all()


async def test_only_missing_or_damaged_cards_are_recomputed(api):
    ix = fr.build(_cat())
    store = MemoryObjectStore()
    find_index.configure(store)
    await find_index.build(ix)
    names = sorted(store.objects)
    del store.objects[names[0]]
    store.objects[names[1]] = b"TRV1" + b"\0" * 10          # damaged
    body = store.objects[names[2]]
    store.objects[names[3]] = body                          # another card's vector under this name
    api["texts"].clear()
    v = await find_index.build(ix)
    assert v.computed == 3 and len(api["texts"]) == 3


async def test_without_a_store_vectors_are_built_in_process_and_a_failing_store_is_no_worse(api):
    ix = fr.build(_cat())
    v = await find_index.build(ix)                          # no store configured
    assert v.computed == len(ix.units)
    store = MemoryObjectStore()
    store.fail_gets, store.fail_puts = True, 10 ** 6
    find_index.configure(store)
    v = await find_index.build(ix)
    assert v.computed == len(ix.units) and v.matrix.shape == (len(ix.units), 5)


async def test_an_api_that_is_down_keeps_the_channel_off_and_is_retried_later(api, monkeypatch):
    cat = _cat()
    ix = fr.build(cat)
    api["down"] = True
    first = await find_index.semantic("make a picture", cat, ix)
    assert first == find_index.Semantic(None, error="not_ready")      # the build starts in the background
    await find_index._build.task
    assert find_index._build.error == "http_503"
    assert (await find_index.semantic("make a picture", cat, ix)).error == "not_ready"
    task = find_index._build.task
    api["down"] = False
    monkeypatch.setattr(find_index, "RETRY_AFTER_S", 0.0)
    await find_index.semantic("make a picture", cat, ix)                # retried
    assert find_index._build.task is not task
    await find_index._build.task
    ready = await find_index.semantic("make a picture", cat, ix)
    assert ready.error is None and len(ready.scores) == len(ix.units)
    best = max(range(len(ix.units)), key=lambda i: ready.scores[i])
    assert "image" in ix.units[best].text.lower()


async def test_the_channel_is_off_without_a_key_and_a_failed_query_vector_is_reported(api, monkeypatch):
    cat = _cat()
    ix = fr.build(cat)
    monkeypatch.setattr(get_settings(), "find_embed_api_key", "", raising=False)
    monkeypatch.setattr(get_settings(), "platform_key_openrouter", "", raising=False)
    assert (await find_index.semantic("x", cat, ix)).error == "off"
    monkeypatch.setattr(get_settings(), "platform_key_openrouter", "or-key", raising=False)
    assert find_index.api_key() == "or-key"                             # treg's OpenRouter key, on OpenRouter
    monkeypatch.setattr(get_settings(), "find_embed_url", "https://embeddings.example/v1", raising=False)
    assert find_index.api_key() == ""
    monkeypatch.setattr(get_settings(), "find_embed_api_key", "test-key", raising=False)
    await find_index.prepare(cat, ix)
    api["down"] = True
    s = await find_index.semantic("an uncached query", cat, ix)
    assert s.scores is None and s.error == "http_503"


async def test_the_semantic_channel_reaches_a_job_no_word_of_the_query_names(api):
    cat = _cat()
    ix = fr.build(cat)
    await find_index.prepare(cat, ix)
    lexical = fr.recall("make a picture", ix, cat.aliases)
    assert "image-gen.flux.generate" not in [c.unit.id for c in lexical]
    sem = await find_index.semantic("make a picture", cat, ix)
    with_meaning = fr.recall("make a picture", ix, cat.aliases, semantic=sem.scores)
    assert "image-gen.flux.generate" in [c.unit.id for c in with_meaning]


def test_stored_vectors_carry_their_card_and_size():
    key = find_index.card_key("a card")
    body = find_index.encode(key, [0.5, -1.0])
    assert list(find_index.decode(key, body)) == [0.5, -1.0]
    assert find_index.decode(find_index.card_key("another"), body) is None
    assert find_index.decode(key, body[:-1]) is None and find_index.decode(key, None) is None
    assert find_index.model_slug("VoyageAI/voyage-4-lite") == "voyageai-voyage-4-lite"


async def test_the_object_store_names_only_find_vectors():
    from tests.fake_object_store import MemoryObstoreSDK
    from treg.infra.object_store import R2ObjectStore

    sdk = MemoryObstoreSDK()
    store = R2ObjectStore(sdk, 1000)
    name = "find-vectors/voyageai-voyage-4-lite/" + "a" * 64
    await store.put_named(name, b"body")
    assert sdk.path == name and await store.get_named(name) == b"body"
    for bad in ("../secrets", "find-vectors/m/" + "a" * 63, "find-vectors/../" + "a" * 64, "a" * 64,
                "find-vectors/M/" + "a" * 64):
        with pytest.raises(ValueError):
            await store.put_named(bad, b"body")
        with pytest.raises(ValueError):
            await store.get_named(bad)
    with pytest.raises(ValueError):
        await store.put_named(name, b"x" * 1001)


async def test_a_failed_batch_is_retried_before_the_build_gives_up(api, monkeypatch):
    """A transient upstream error in one batch of many must not fail the whole build."""
    ix = fr.build(_cat())
    monkeypatch.setattr(find_index, "BATCH", 4)
    real = embed_infra.embed
    fails = {"left": 2, "error": "http_502", "calls": 0}

    async def flaky(texts, **kw):
        fails["calls"] += 1
        if fails["calls"] >= 2 and fails["left"]:   # the first batch answers, then the next fails
            fails["left"] -= 1
            return embed_infra.Embedding(vectors=None, ms=5, error=fails["error"])
        return await real(texts, **kw)
    monkeypatch.setattr(embed_infra, "embed", flaky)
    v = await find_index.build(ix)                                # batch 2 fails twice, then answers
    assert v.computed == len(ix.units) and fails["calls"] == -(-len(ix.units) // 4) + 2

    # a third failure of the same batch fails the build, keeping what it paid for
    store = MemoryObjectStore()
    find_index.configure(store)
    fails.update(left=3, calls=0)
    with pytest.raises(RuntimeError, match="http_502"):
        await find_index.build(ix)
    assert fails["calls"] == 4 and len(store.objects) == 4       # batch 1 written, batch 2 tried 3 times

    # a refused key is not retried
    fails.update(left=10, calls=1, error="http_401")
    with pytest.raises(RuntimeError, match="http_401"):
        await find_index.build(fr.build(_cat()))
    assert fails["calls"] == 2


async def test_warm_builds_the_shipped_catalog_at_startup_and_a_cancel_reaches_the_build(api, monkeypatch):
    """`warm` is the lifespan's background task: it parses the catalog off the loop, builds on the
    one cached object, and awaits the build so cancelling the task cancels the build too."""
    cat = _cat()
    monkeypatch.setattr(catalog_store, "load", lambda **kw: cat)
    await find_index.warm()
    assert find_index._build is not None and find_index._build.cat is cat
    assert find_index._build.vectors is not None and api["texts"]
    assert (await find_index.semantic("make a picture", cat, fr.index(cat))).error is None

    find_index.reset()
    api["texts"].clear()
    gate = asyncio.Event()

    async def slow_embed(texts, **kw):
        await gate.wait()
        return embed_infra.Embedding(vectors=[_vector(t) for t in texts], ms=5)
    monkeypatch.setattr(embed_infra, "embed", slow_embed)
    task = asyncio.create_task(find_index.warm())
    await asyncio.sleep(0)
    while find_index._build is None or find_index._build.task is None:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert find_index._build.task.cancelled()
    assert find_index._build.vectors is None


async def test_warm_is_a_no_op_without_an_embedding_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "find_embed_api_key", "", raising=False)
    monkeypatch.setattr(get_settings(), "platform_key_openrouter", "", raising=False)
    find_index.reset()
    await find_index.warm()
    assert find_index._build is None
