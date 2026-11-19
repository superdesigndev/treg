"""Re-encrypt stored Fernet payloads under the first key of `TREG_SECRET_KEY`.

Run by `treg-worker admin rotate-fernet` while the setting lists the new key first and the old key
after it. It covers Arena payloads and every stored credential still on Fernet (all of them on a
deployment without KMS); KMS-sealed credentials are left as they are. Demo and sandbox seeds are left
behind on purpose; they stop opening once the old key is dropped.

Each batch reads a bounded id range, re-encrypts locally, then writes every row only if its payload
is still the one it read, so a payload updated in between is left for the next run.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select, update

from .. import crypto
from ..infra import credential_cipher
from ..infra.db import session_maker
from ..models import ArenaEvaluation, ArenaRun, Org, Secret, WebArenaRun

# (model, encrypted column, first id, extra filter) — credentials of demo and sandbox teams excluded.
TARGETS = (
    (ArenaRun, ArenaRun.payload, "", ()),
    (WebArenaRun, WebArenaRun.payload, "", ()),
    (ArenaEvaluation, ArenaEvaluation.payload, "", ()),
    (Secret, Secret.value, 0, (or_(Org.id.is_(None), Org.demo.is_(False)),)),
)


async def rotate(*, batch_size: int = 200, dry_run: bool = False, session_factory=session_maker) -> dict:
    """→ {<table>: {scanned, rotated, current, changed, unreadable}, error}. `dry_run` writes nothing.
    A KMS-sealed credential counts as current."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    out: dict = {}
    try:
        for model, column, after, extra in TARGETS:
            counts = out[model.__tablename__] = dict.fromkeys(
                ("scanned", "rotated", "current", "changed", "unreadable"), 0)
            while True:
                query = select(model.id, column.label("payload")).where(model.id > after, *extra)
                if model is Secret:
                    query = query.outerjoin(Org, Org.id == Secret.org_id)
                async with session_factory() as db:
                    rows = (await db.execute(query.order_by(model.id).limit(batch_size))).all()
                if not rows:
                    break
                after = rows[-1].id
                counts["scanned"] += len(rows)
                writes = []
                for row in rows:
                    if credential_cipher.is_kms_sealed(row.payload) or crypto.is_current(row.payload):
                        counts["current"] += 1
                        continue
                    try:
                        writes.append((row.id, row.payload, crypto.rotate(row.payload)))
                    except Exception:  # noqa: BLE001 — counted; one bad row must not stop the run
                        counts["unreadable"] += 1
                if dry_run:
                    counts["rotated"] += len(writes)
                    continue
                async with session_factory() as db:
                    for row_id, old, new in writes:
                        result = await db.execute(
                            update(model).where(model.id == row_id, column == old)
                            .values({column.key: new}).execution_options(synchronize_session=False))
                        counts["rotated" if result.rowcount else "changed"] += 1
                    await db.commit()
    except Exception as exc:  # noqa: BLE001 — reported, and the worker exits non-zero on it
        logging.getLogger("treg").warning("Fernet rotation failed: %s", exc)
        return {**out, "error": str(exc)}
    return {**out, "error": None}
