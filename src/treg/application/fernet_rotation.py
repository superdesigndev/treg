"""Re-encrypt stored Fernet payloads under the first key of `TREG_SECRET_KEY`.

Run by `treg-worker admin rotate-fernet` while the setting lists the new key first and the old key
after it. Stored credentials are not touched here: they are sealed through `credential_cipher`, and
`reencrypt-credentials` moves any Fernet credential of a real team to KMS. Demo and sandbox seeds are
left behind on purpose; they stop opening once the old key is dropped.

Each batch reads a bounded id range, re-encrypts locally, then writes every row only if its payload
is still the one it read, so a payload updated in between is left for the next run.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update

from .. import crypto
from ..infra.db import session_maker
from ..models import ArenaEvaluation, ArenaRun, WebArenaRun

MODELS = (ArenaRun, WebArenaRun, ArenaEvaluation)


async def rotate(*, batch_size: int = 200, dry_run: bool = False, session_factory=session_maker) -> dict:
    """→ {<table>: {scanned, rotated, current, changed, unreadable}, error}. `dry_run` writes nothing."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    out: dict = {}
    try:
        for model in MODELS:
            counts = out[model.__tablename__] = dict.fromkeys(
                ("scanned", "rotated", "current", "changed", "unreadable"), 0)
            after = ""
            while True:
                async with session_factory() as db:
                    rows = (await db.execute(
                        select(model.id, model.payload).where(model.id > after)
                        .order_by(model.id).limit(batch_size))).all()
                if not rows:
                    break
                after = rows[-1].id
                counts["scanned"] += len(rows)
                writes = []
                for row in rows:
                    if crypto.is_current(row.payload):
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
                            update(model).where(model.id == row_id, model.payload == old)
                            .values(payload=new).execution_options(synchronize_session=False))
                        counts["rotated" if result.rowcount else "changed"] += 1
                    await db.commit()
    except Exception as exc:  # noqa: BLE001 — reported, and the worker exits non-zero on it
        logging.getLogger("treg").warning("Fernet rotation failed: %s", exc)
        return {**out, "error": str(exc)}
    return {**out, "error": None}
