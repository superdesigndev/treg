"""Disposable dashboard browser-test server; never an application entry point."""
from __future__ import annotations

import asyncio
import os

from treg.__main__ import _prepare_serve
from treg.application import auth
from treg.config import get_settings


if __name__ == "__main__":
    import uvicorn

    if not get_settings().expose_dev_code:
        raise RuntimeError("Browser tests require local SQLite and development email codes.")
    # Each scenario verifies a fresh inbox, but all browsers share one loopback IP.
    # Keep real OTP verification and per-inbox limits; enlarge only this test process's
    # shared IP budget. Production imports neither this module nor this override.
    auth.OTP_START_MAX_PER_IP = 256
    asyncio.run(_prepare_serve())
    uvicorn.run("treg.api:app", host="127.0.0.1", port=int(os.environ["TREG_E2E_PORT"]))
