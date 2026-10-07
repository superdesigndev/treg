"""Vibe-it: a maker and treg's agent build a hub tool in conversation
(docs/context/architecture/vibe-it.md).

The only writer of `VibeSession`, `VibeMessage`, `VibeDraft` and `VibeBudget`. Everything the agent does to the
team (validate, test run, publish, turn on an app) goes through the hub's own routes as the
signed-in maker, so it can do nothing the maker could not. treg pays for the model, up to
`vibe_budget_usd` per person; a test run's steps are charged to the maker's team as always.
Functions here never commit unless they say so: the route does.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...models import VibeBudget, VibeDraft, VibeMessage, VibeSession
from ...timeutil import utcnow_naive
from ..hub import enabled_for as hub_enabled_for

DRAFT_KEYS = ("manifest", "script", "check", "readme", "data")
TITLE_MAX = 80
TEXT_MAX = 8000
ATTACH_MAX = 200_000          # characters across one message's attachments
DATA_MAX = 5_000_000          # data.csv, the hub's own cap
RESULT_SHOWN = 4000           # characters of a step's result the page can expand
RUN_STALE = timedelta(minutes=15)   # an agent marked running longer than this died with its process


def enabled_for(org_slug: str | None, email: str | None = None) -> bool:
    """Vibe-it is on AND the hub is on for this reader."""
    return bool(get_settings().vibe_enabled) and hub_enabled_for(org_slug, email)


def budget_micro() -> int:
    return int(round(max(0.0, float(get_settings().vibe_budget_usd)) * 1_000_000))


async def spent_micro(db: AsyncSession, user_id: int) -> int:
    row = await db.get(VibeBudget, user_id)
    return int(row.spent_micro) if row else 0


async def left_micro(db: AsyncSession, user_id: int) -> int:
    return max(0, budget_micro() - await spent_micro(db, user_id))


async def spend(db: AsyncSession, user_id: int, micro: int) -> None:
    """Record treg's model spend on this person. Never below zero; never restored. One atomic
    `spent = spent + n` in the database, so conversations spending at once never lose each other's
    amount; the first spend inserts the row (a racing first insert retries as the update)."""
    if micro <= 0:
        return
    bump = (update(VibeBudget).where(VibeBudget.user_id == user_id)
            .values(spent_micro=VibeBudget.spent_micro + micro, updated_at=utcnow_naive()))
    if ((await db.execute(bump)).rowcount or 0) == 1:
        return
    try:
        async with db.begin_nested():
            db.add(VibeBudget(user_id=user_id, spent_micro=micro, updated_at=utcnow_naive()))
    except IntegrityError:
        await db.execute(bump)


def clean_draft(draft: Any) -> dict[str, Any]:
    """The four files and nothing else: manifest and check are objects, script and readme text."""
    d = draft if isinstance(draft, dict) else {}
    out: dict[str, Any] = {}
    for k in ("manifest", "check"):
        if isinstance(d.get(k), dict):
            out[k] = d[k]
    for k in ("script", "readme"):
        if isinstance(d.get(k), str):
            out[k] = d[k][:200_000]
    if isinstance(d.get("data"), str) and len(d["data"]) <= DATA_MAX:
        out["data"] = d["data"]
    return out


async def create(db: AsyncSession, *, user_id: int, org_id: int, title: str = "") -> VibeSession:
    now = utcnow_naive()
    row = VibeSession(user_id=user_id, org_id=org_id, title=(title or "New tool")[:TITLE_MAX], draft={},
                      created_at=now, updated_at=now)
    db.add(row)
    await db.flush()
    return row


async def of_user(db: AsyncSession, *, user_id: int, session_id: int) -> VibeSession | None:
    row = await db.get(VibeSession, session_id)
    return row if row is not None and row.user_id == user_id else None


async def list_for(db: AsyncSession, *, user_id: int, org_id: int) -> list[VibeSession]:
    return list((await db.execute(select(VibeSession).where(
        VibeSession.user_id == user_id, VibeSession.org_id == org_id)
        .order_by(VibeSession.updated_at.desc()).limit(100))).scalars().all())


async def drafts_of(db: AsyncSession, *, user_id: int, org_id: int, exclude: int | None = None) -> list[VibeSession]:
    """This person's unpublished drafts in this team: conversations with files and no tool yet."""
    rows = await list_for(db, user_id=user_id, org_id=org_id)
    return [r for r in rows if r.id != exclude and not r.tool_id and clean_draft(r.draft)]


async def copy_draft(db: AsyncSession, session: VibeSession, source: VibeSession) -> VibeDraft | None:
    """Continue a draft in another conversation: its files are copied in as a new version here.
    A copy, not a link: the two conversations no longer affect each other."""
    return await set_draft(db, session, source.draft, author="load", note=f"copied from the draft “{source.title}”",
                           replace=True)


async def messages(db: AsyncSession, session_id: int) -> list[VibeMessage]:
    return list((await db.execute(select(VibeMessage).where(VibeMessage.session_id == session_id)
                                  .order_by(VibeMessage.id))).scalars().all())


async def add_message(db: AsyncSession, session: VibeSession, role: str, content: dict, cost_micro: int = 0) -> VibeMessage:
    msg = VibeMessage(session_id=session.id, role=role, content=content, cost_micro=cost_micro, created_at=utcnow_naive())
    db.add(msg)
    session.updated_at = utcnow_naive()
    await db.flush()
    return msg


async def set_draft(db: AsyncSession, session: VibeSession, draft: Any, *, author: str,
                    note: str = "", replace: bool = False) -> VibeDraft | None:
    """Change the files and keep the version. `replace` takes `draft` as the whole set (a load or a
    restore); otherwise omitted files keep their content. Returns the new version, or None when
    nothing changed."""
    files = clean_draft(draft) if replace else {**clean_draft(session.draft), **clean_draft(draft)}
    if files == clean_draft(session.draft):
        return None
    session.draft = files
    name = (files.get("manifest") or {}).get("name")
    if isinstance(name, str) and name and session.title in ("", "New tool"):
        session.title = name[:TITLE_MAX]
    session.updated_at = utcnow_naive()
    n = await latest_n(db, session.id) or 0
    last = (await db.execute(select(func.max(VibeMessage.id)).where(VibeMessage.session_id == session.id))).scalar()
    row = VibeDraft(session_id=session.id, n=n + 1, files=await _stored(db, session.id, n, files), author=author,
                    note=note[:200], message_id=last, created_at=utcnow_naive())
    db.add(row)
    await db.flush()
    return row


# data.csv (up to DATA_MAX) is kept once, not in every version: a version whose data is the same as
# the version before stores only its digest and the number of the version that holds the text.
_DATA_SHA, _DATA_OF = "__data_sha", "__data_of"


async def _stored(db: AsyncSession, session_id: int, prev_n: int, files: dict) -> dict:
    if "data" not in files:
        return files
    sha = hashlib.sha256(files["data"].encode()).hexdigest()
    prev = await version(db, session_id, prev_n) if prev_n else None
    if prev is not None and (prev.files or {}).get(_DATA_SHA) == sha:
        holder = (prev.files or {}).get(_DATA_OF) or prev.n
        return {**{k: v for k, v in files.items() if k != "data"}, _DATA_SHA: sha, _DATA_OF: holder}
    return {**files, _DATA_SHA: sha}


async def files_of(db: AsyncSession, v: VibeDraft) -> dict[str, Any]:
    """A version's files as the maker wrote them (data.csv fetched from the version that keeps it)."""
    files = dict(v.files or {})
    holder = files.pop(_DATA_OF, None)
    files.pop(_DATA_SHA, None)
    if holder is not None:
        src = await version(db, v.session_id, int(holder))
        if src is not None and isinstance((src.files or {}).get("data"), str):
            files["data"] = src.files["data"]
    return files


async def latest_n(db: AsyncSession, session_id: int) -> int | None:
    return (await db.execute(select(func.max(VibeDraft.n)).where(VibeDraft.session_id == session_id))).scalar()


async def versions(db: AsyncSession, session_id: int) -> list[VibeDraft]:
    """Every version, newest first, without loading their files (the list never shows them)."""
    return list((await db.execute(select(VibeDraft).options(defer(VibeDraft.files))
                                  .where(VibeDraft.session_id == session_id)
                                  .order_by(VibeDraft.n.desc()))).scalars().all())


async def version(db: AsyncSession, session_id: int, n: int) -> VibeDraft | None:
    return (await db.execute(select(VibeDraft).where(VibeDraft.session_id == session_id, VibeDraft.n == n))).scalars().first()


async def restore(db: AsyncSession, session: VibeSession, n: int) -> VibeDraft | None:
    """Bring back version `n` as a new version (history only grows). None when `n` is unknown or
    already what the files are."""
    old = await version(db, session.id, n)
    if old is None:
        return None
    return await set_draft(db, session, await files_of(db, old), author="restore", note=f"restored draft {n}",
                           replace=True)


async def mark_published(db: AsyncSession, session: VibeSession, hub_version: int) -> None:
    """The files as they stand became the hub tool's version `hub_version`. Every change to the
    files makes a version (`set_draft`), so the newest version is the files as they stand."""
    n = await latest_n(db, session.id)
    if n is not None:
        await db.execute(update(VibeDraft).where(VibeDraft.session_id == session.id, VibeDraft.n == n)
                         .values(published_version=hub_version))


async def files_before(db: AsyncSession, session: VibeSession, message_id: int) -> dict[str, Any]:
    """The files as they stood when message `message_id` was sent (a regenerate goes back to them)."""
    row = (await db.execute(select(VibeDraft).where(
        VibeDraft.session_id == session.id, or_(VibeDraft.message_id.is_(None), VibeDraft.message_id <= message_id))
        .order_by(VibeDraft.n.desc()).limit(1))).scalars().first()
    return await files_of(db, row) if row else {}


def view_version(v: VibeDraft, files: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"n": v.n, "author": v.author, "note": v.note, "published_version": v.published_version,
                           "at": v.created_at.isoformat()}
    if files is not None:
        out["files"] = files
    return out


async def set_tool(db: AsyncSession, session: VibeSession, tool_id: str | None) -> None:
    """The hub tool this conversation published or loaded."""
    session.tool_id = tool_id
    await db.flush()


async def set_pending(db: AsyncSession, session: VibeSession, pending: dict | None) -> None:
    session.pending = pending
    await db.flush()


async def update_meta(db: AsyncSession, session: VibeSession, *, title: str | None = None,
                      pinned: bool | None = None, auto_test: bool | None = None) -> None:
    if title is not None and title.strip():
        session.title = title.strip()[:TITLE_MAX]
    if pinned is not None:
        session.pinned = pinned
    if auto_test is not None:
        session.auto_test = auto_test
    await db.flush()


def running(session: VibeSession) -> bool:
    return session.running_since is not None and session.running_since > utcnow_naive() - RUN_STALE


async def claim_run(db: AsyncSession, session_id: int) -> str | None:
    """Mark the agent at work on this conversation, unless it already is (on any instance): the
    run's token, or None. A mark not refreshed for `RUN_STALE` belonged to a process that died; a
    live run refreshes it every turn and step (`touch_run`)."""
    now = utcnow_naive()
    token = secrets.token_hex(8)
    r = await db.execute(update(VibeSession).where(
        VibeSession.id == session_id,
        or_(VibeSession.running_since.is_(None), VibeSession.running_since < now - RUN_STALE))
        .values(running_since=now, run_token=token, stop_requested=False))
    return token if (r.rowcount or 0) == 1 else None


async def touch_run(db: AsyncSession, session_id: int, token: str) -> bool:
    """Refresh this run's mark. False when another run holds it now (this one went stale)."""
    r = await db.execute(update(VibeSession).where(VibeSession.id == session_id, VibeSession.run_token == token)
                         .values(running_since=utcnow_naive()))
    return (r.rowcount or 0) == 1


async def release_run(db: AsyncSession, session_id: int, token: str) -> None:
    """Clear the mark, only if this run still holds it: a stale run never clears a newer one's."""
    await db.execute(update(VibeSession).where(VibeSession.id == session_id, VibeSession.run_token == token)
                     .values(running_since=None, run_token=None, stop_requested=False))


async def request_stop(db: AsyncSession, session_id: int) -> None:
    await db.execute(update(VibeSession).where(VibeSession.id == session_id, VibeSession.running_since.is_not(None))
                     .values(stop_requested=True))


async def stop_wanted(db: AsyncSession, session_id: int) -> bool:
    return bool((await db.execute(select(VibeSession.stop_requested).where(VibeSession.id == session_id))).scalar())


async def last_user_message(db: AsyncSession, session_id: int) -> VibeMessage | None:
    return (await db.execute(select(VibeMessage).where(VibeMessage.session_id == session_id, VibeMessage.role == "user")
                             .order_by(VibeMessage.id.desc()).limit(1))).scalars().first()


async def drop_answer(db: AsyncSession, session_id: int, after_id: int) -> None:
    """A regenerate: the agent's turns and steps after the maker's message go. The maker's own
    actions (events: test runs, publishes) stay, because they happened."""
    await db.execute(delete(VibeMessage).where(VibeMessage.session_id == session_id, VibeMessage.id > after_id,
                                               VibeMessage.role.in_(("assistant", "tool"))))


async def remove(db: AsyncSession, session: VibeSession) -> None:
    await db.execute(delete(VibeMessage).where(VibeMessage.session_id == session.id))
    await db.execute(delete(VibeDraft).where(VibeDraft.session_id == session.id))
    await db.delete(session)
    await db.flush()


async def forget_user(db: AsyncSession, user_id: int) -> None:
    """A person is deleted: their conversations and their budget row go too."""
    ids = select(VibeSession.id).where(VibeSession.user_id == user_id)
    await db.execute(delete(VibeMessage).where(VibeMessage.session_id.in_(ids)))
    await db.execute(delete(VibeDraft).where(VibeDraft.session_id.in_(ids)))
    await db.execute(delete(VibeSession).where(VibeSession.user_id == user_id))
    await db.execute(delete(VibeBudget).where(VibeBudget.user_id == user_id))
    await db.flush()


def view_session(s: VibeSession) -> dict[str, Any]:
    return {"id": s.id, "title": s.title, "tool_id": s.tool_id, "updated_at": s.updated_at.isoformat(),
            "trimmed": bool(s.summary), "pinned": bool(s.pinned), "busy": running(s),
            "has_files": bool(clean_draft(s.draft)), "name": (clean_draft(s.draft).get("manifest") or {}).get("name")}


def view_message(m: VibeMessage) -> dict[str, Any]:
    """What the page shows: the maker's text and attachments, the agent's text and cost, each tool
    step with its arguments and result, and each action the maker took (an event)."""
    c = m.content or {}
    out: dict[str, Any] = {"id": m.id, "role": m.role, "at": m.created_at.isoformat()}
    if m.role == "tool":
        result = c.get("result") or ""
        out.update({"name": c.get("name"), "summary": c.get("summary") or "", "ok": c.get("ok", True),
                    "args": c.get("arguments"), "result": result[:RESULT_SHOWN], "version": c.get("version"),
                    "prev_version": c.get("prev_version")})
    elif m.role == "event":
        out.update({k: c.get(k) for k in ("kind", "summary", "ok", "detail", "cost_micro", "inputs", "tool_id",
                                           "version", "status", "url")})
    else:
        out["text"] = c.get("text") or ""
        if m.role == "assistant":
            out["cost_micro"] = int(m.cost_micro or 0)
            if c.get("stopped"):
                out["stopped"] = True
            if c.get("tool_calls"):
                out["calls"] = [t.get("name") for t in c["tool_calls"]]
        if c.get("attachments"):
            out["attachments"] = [{"name": a.get("name"), "size": len(a.get("text") or "")} for a in c["attachments"]]
    return out


async def trim_idle(db: AsyncSession, *, now=None, batch: int = 100) -> int:
    """Option C of the plan: a conversation idle for `vibe_trim_after_days` keeps its files and their
    versions, its published tool and a short summary; its messages go. No model call: the summary is
    the maker's first ask and the agent's last answer, after any earlier summary, so a conversation
    resumed after a trim is trimmed again when it goes idle again. At most `batch` conversations per
    call (the worker calls until none is left). Returns how many were trimmed. Does not commit."""
    cutoff = (now or utcnow_naive()) - timedelta(days=max(1, int(get_settings().vibe_trim_after_days)))
    has_messages = select(VibeMessage.id).where(VibeMessage.session_id == VibeSession.id).exists()
    rows = (await db.execute(select(VibeSession).where(VibeSession.updated_at < cutoff, has_messages)
                             .order_by(VibeSession.id).limit(batch))).scalars().all()
    for s in rows:
        msgs = await messages(db, s.id)
        first = next((m.content.get("text", "") for m in msgs if m.role == "user"), "")
        last = next((m.content.get("text", "") for m in reversed(msgs) if m.role == "assistant" and m.content.get("text")), "")
        earlier = f"{s.summary[:1200]}\nThen, " if s.summary else ""
        s.summary = (f"{earlier}Earlier in this conversation the maker asked: {first[:1200]}\n"
                     f"The agent's last answer was: {last[:1200]}")[:4000]
        await db.execute(delete(VibeMessage).where(VibeMessage.session_id == s.id))
    await db.flush()
    return len(rows)
