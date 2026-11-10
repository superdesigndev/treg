"""Minute/worker-exit admission aggregates; never called from an individual transaction."""

from __future__ import annotations

import json
import logging


def emit_snapshot(*, role: str, shutdown: bool = False) -> int:
    from .. import analytics
    from . import money_admission

    try:
        rows = money_admission.snapshot()
    except Exception:  # noqa: BLE001 - observing admission must never affect accounting
        return 0
    for row in rows:
        try:
            props = {**row, "role": role, "shutdown": shutdown, "build": analytics.build_id()}
        except Exception:  # noqa: BLE001
            continue
        # Short workers can exit before analytics ships a batch. Keep the same bounded summary
        # locally so missing analytics transport is distinguishable from missing accounting.
        try:
            logging.getLogger("treg.money_admission").warning(
                "money_admission_gauge %s", json.dumps(props, separators=(",", ":")))
        except Exception:  # noqa: BLE001
            pass
        try:
            analytics.capture(analytics.SERVER_DISTINCT_ID, "money_admission_gauge", props)
        except Exception:  # noqa: BLE001
            pass
    return len(rows)
