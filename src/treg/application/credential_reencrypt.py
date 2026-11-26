"""Re-seal stored credentials under KMS, and record `Secret.refreshable` where it is missing.

Run by `treg-worker admin reencrypt-credentials`, never on a request. It refuses to run until
this process seals new writes with KMS (`TREG_KMS_ENCRYPT_NEW`): by then every service reading
credentials must already hold the key, or the values it writes would be unreadable to them.

Each batch reads a bounded id range, opens and seals with no transaction held, then writes every
row back only if its value is still the one it read, so a credential refreshed or replaced in
between is left alone and picked up by the next run. Demo and sandbox seeds stay on Fernet: they
are published placeholder values, not credentials.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select, update

from .. import crypto
from ..domain.connections import refresh as connection_refresh
from ..infra import credential_cipher
from ..infra.db import session_maker
from ..models import Org, Secret


class KmsWritesOff(RuntimeError):
    pass


async def reencrypt(*, batch_size: int = 200, dry_run: bool = False, session_factory=session_maker) -> dict:
    """→ {scanned, resealed, flagged, changed, unreadable, error}. `dry_run` counts what a run would
    do and writes nothing; it neither needs nor calls KMS."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if not dry_run and not credential_cipher.kms_writes():
        raise KmsWritesOff("re-encryption needs TREG_KMS_ENCRYPT_NEW on this process")
    counts = {"scanned": 0, "resealed": 0, "flagged": 0, "changed": 0, "unreadable": 0}
    after = 0
    try:
        while True:
            async with session_factory() as db:
                rows = (await db.execute(
                    select(Secret.id, Secret.org_id, Secret.kind, Secret.value, Secret.refreshable)
                    .outerjoin(Org, Org.id == Secret.org_id)
                    .where(Secret.id > after, or_(Org.id.is_(None), Org.demo.is_(False)))
                    .order_by(Secret.id).limit(batch_size))).all()
            if not rows:
                break
            after = rows[-1].id
            counts["scanned"] += len(rows)
            writes = []
            for row in rows:
                legacy = not credential_cipher.is_kms_sealed(row.value)
                needs_flag = row.kind == "oauth" and row.refreshable is None
                if not legacy and not needs_flag:
                    continue
                if legacy:
                    try:
                        plaintext = crypto.decrypt(row.value)
                    except Exception:  # noqa: BLE001 — counted; one bad row must not stop the run
                        counts["unreadable"] += 1
                        continue
                elif dry_run:
                    writes.append((row, {"refreshable": None}))
                    continue
                else:
                    plaintext = await credential_cipher.unseal(row.value, org_id=row.org_id)
                values = {}
                if legacy:
                    values["value"] = row.value if dry_run else await credential_cipher.seal(
                        plaintext, org_id=row.org_id)
                if needs_flag:
                    values["refreshable"] = connection_refresh.refreshable_flag(row.kind, plaintext)
                writes.append((row, values))
            if dry_run:
                counts["resealed"] += sum(1 for _, v in writes if "value" in v)
                counts["flagged"] += sum(1 for _, v in writes if "refreshable" in v)
                continue
            async with session_factory() as db:
                for row, values in writes:
                    result = await db.execute(
                        update(Secret).where(Secret.id == row.id, Secret.value == row.value)
                        .values(**values).execution_options(synchronize_session=False))
                    if not result.rowcount:
                        counts["changed"] += 1
                        continue
                    counts["resealed"] += "value" in values
                    counts["flagged"] += "refreshable" in values
                await db.commit()
    except Exception as exc:  # noqa: BLE001 — reported, and the worker exits non-zero on it
        logging.getLogger("treg").warning("credential re-encryption failed: %s", exc)
        return {**counts, "error": str(exc)}
    return {**counts, "error": None}
