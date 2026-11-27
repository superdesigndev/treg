"""The /follower-analysis board: every account a visitor analysed, kept for every later visitor.

The scan runs in the visitor's browser (ordinary /call/s on their own team), so the server only keeps
the finished result. Each account is one row in the shared key-value store, keyed by its handle, and
one small index row lists them all, so the page draws its table from the index and fetches a full
result only when someone opens it. The same handle is never analysed twice: a later save replaces the
stored one only when it read more of the follower list, or when the same visitor finishes the full
read of their own scan.
"""

from __future__ import annotations

import json
import re
import time

from sqlalchemy.ext.asyncio import AsyncSession

from .. import ratestore

NS, INDEX = "follower_board", "_index"
TTL_S = 400 * 86400
MAX_ENTRIES = 500
MAX_PEOPLE = 140
MAX_BYTES = 120_000
KINDS = ("person", "company")

_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_PERSON_FIELDS = {"handle": str, "name": str, "avatar": str, "followers": int, "bio": str, "cat": str,
                  "level": str, "default_avatar": bool, "org": bool}
_TEXT_CAP = {"bio": 400, "name": 80, "avatar": 300, "handle": 15, "cat": 12, "level": 24}


class BoardError(ValueError):
    """A result the board will not keep, with the reason."""


def key(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def _person(row: object) -> dict:
    if not isinstance(row, dict) or not _HANDLE.match(str(row.get("handle") or "")):
        raise BoardError("a follower row has no valid handle")
    out = {}
    for name, kind in _PERSON_FIELDS.items():
        v = row.get(name)
        if v is None or v == "":
            continue
        if kind is int:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise BoardError(f"{name} must be a non-negative number")
            out[name] = int(v)
        elif kind is bool:
            out[name] = bool(v)
        else:
            out[name] = str(v)[: _TEXT_CAP.get(name, 200)]
    if out.get("avatar") and not out["avatar"].startswith("https://pbs.twimg.com/"):
        out.pop("avatar")
    return out


def clean(body: object) -> dict:
    """The stored shape of a finished scan, or BoardError. Only the fields the page draws are kept,
    every text is capped and photos must be X's own image host."""
    if not isinstance(body, dict):
        raise BoardError("expected a JSON object")
    handle = str(body.get("handle") or "").lstrip("@")
    if not _HANDLE.match(handle):
        raise BoardError("handle must be a valid X handle")
    cats = body.get("cats")
    if not isinstance(cats, list) or not 1 <= len(cats) <= 6:
        raise BoardError("cats must list one to six groups")
    groups = []
    for c in cats:
        if not isinstance(c, dict) or not re.match(r"^c\d$", str(c.get("key") or "")) or not str(c.get("name") or "").strip():
            raise BoardError("each group needs a key and a name")
        groups.append({"key": c["key"], "name": str(c["name"])[:40], "desc": str(c.get("desc") or "")[:160]})
    sample, notable = body.get("sample"), body.get("notable") or []
    if not isinstance(sample, list) or not 1 <= len(sample) <= MAX_PEOPLE:
        raise BoardError(f"sample must hold 1 to {MAX_PEOPLE} followers")
    if not isinstance(notable, list) or len(notable) > 12:
        raise BoardError("notable holds at most 12 accounts")
    owner = body.get("owner") if isinstance(body.get("owner"), dict) else {}
    image = str(owner.get("image") or "")
    facts = body.get("facts") if isinstance(body.get("facts"), dict) else None
    if facts is not None:
        facts = {k: v for k, v in facts.items() if k in (
            "n", "read", "exact", "recent", "verified", "veteran", "big", "year", "places", "word", "reach")}
        if not isinstance(facts.get("n"), int) or not isinstance(facts.get("read"), int):
            raise BoardError("facts must say how many followers there are and how many were read")
        facts["places"] = [str(p)[:30] for p in (facts.get("places") or [])][:3]
    kind = body.get("kind") if body.get("kind") in KINDS else "person"
    out = {
        "v": 2, "handle": handle, "kind": kind, "cats": groups, "unread": max(0, int(body.get("unread") or 0)),
        "owner": {"name": str(owner.get("name") or "")[:80], "image": image if image.startswith("https://pbs.twimg.com/") else "",
                  "followers": max(0, int(owner.get("followers") or 0))},
        "sample": [_person(r) for r in sample], "notable": [_person(r) for r in notable], "facts": facts,
    }
    if len(json.dumps(out)) > MAX_BYTES:
        raise BoardError("the result is too large")
    return out


def summary(entry: dict) -> dict:
    """What the table row needs: no bios, and only the three biggest followers."""
    counts: dict[str, int] = {}
    for r in entry["sample"]:
        if r.get("cat"):
            counts[r["cat"]] = counts.get(r["cat"], 0) + 1
    f = entry.get("facts") or {}
    return {"handle": entry["handle"], "kind": entry.get("kind", "person"), "at": entry["at"],
            "name": entry["owner"].get("name", ""), "image": entry["owner"].get("image", ""),
            "followers": f.get("n") or entry["owner"].get("followers", 0), "exact": bool(f.get("exact")),
            "cats": [{"key": c["key"], "name": c["name"]} for c in entry["cats"]], "n": len(entry["sample"]), "counts": counts,
            "notable": [{"handle": r["handle"], "avatar": r.get("avatar", ""), "followers": r.get("followers", 0)} for r in entry["notable"][:3]]}


async def board(db: AsyncSession) -> list[dict]:
    index = await ratestore.kv_get(db, NS, INDEX)
    return list((index or {}).get("rows") or [])


async def entry(db: AsyncSession, handle: str) -> dict | None:
    return await ratestore.kv_get(db, NS, key(handle))


async def save(db: AsyncSession, result: dict, user_id: int) -> dict:
    """Keep a finished scan. A handle already on the board keeps its stored result unless this one
    read more of the follower list, or the visitor who saved it is finishing their own full read."""
    def read(e: dict) -> tuple[int, bool]:
        f = e.get("facts") or {}
        return f.get("read") or 0, bool(f.get("exact"))

    old = await entry(db, result["handle"])
    if old is not None:
        if not (read(result) > read(old) or (old.get("by") == user_id and read(result) >= read(old))):
            return {**old, "kept": True}
    stored = {**result, "at": int(time.time() * 1000), "by": user_id}
    await ratestore.kv_put(db, NS, key(result["handle"]), stored, ttl_s=TTL_S)
    rows = [r for r in await board(db) if key(r["handle"]) != key(result["handle"])]
    rows.insert(0, summary(stored))
    await ratestore.kv_put(db, NS, INDEX, {"rows": rows[:MAX_ENTRIES]}, ttl_s=TTL_S)
    return stored


def public(entry: dict) -> dict:
    """A stored result without who saved it."""
    return {k: v for k, v in entry.items() if k != "by"}


def share_text(row: dict) -> tuple[str, str]:
    """The title and description a shared link shows: whose followers, the biggest groups, and the
    biggest follower when there is one."""
    names = {c["key"]: c["name"] for c in row.get("cats") or []}
    n = max(1, int(row.get("n") or 1))
    top = sorted((row.get("counts") or {}).items(), key=lambda kv: -kv[1])[:3]
    mix = ", ".join(f"{round(100 * k / n)}% {names.get(c, c)}" for c, k in top if c in names)
    big = (row.get("notable") or [None])[0]
    title = f"Who follows @{row['handle']}"
    desc = mix + (f". Followed by @{big['handle']}." if big else ".") if mix else "Their followers, sorted by Jev."
    return title, desc + " Analysed with treg × Jev."
