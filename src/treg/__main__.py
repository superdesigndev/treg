"""`python -m treg` - run the server and its explicit maintenance commands."""

from __future__ import annotations

import os
import sys


async def _prepare_serve() -> None:
    """Apply release maintenance, then provision this local box when single-user mode allows it."""
    # Both imports are deliberately lazy so `python -m treg keygen` stays on the light path.
    from . import api, maintenance
    from .infra.db import dispose_engine

    await maintenance.upgrade()
    await api._bootstrap_single_user()
    await dispose_engine()


async def _run_upgrade() -> None:
    from . import maintenance
    from .infra.db import dispose_engine

    await maintenance.upgrade()
    await dispose_engine()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "keygen":
        from .crypto import new_key

        print(new_key())
        return

    if len(sys.argv) > 1 and sys.argv[1] == "upgrade":
        import asyncio

        asyncio.run(_run_upgrade())
        print("treg upgrade complete")
        return

    import asyncio
    import uvicorn

    asyncio.run(_prepare_serve())

    # Honor $PORT (Render/Heroku set it and route/health-check that port) — a hard-coded port makes
    # the deploy unreachable. Falls back to the local dev port.
    port = int(os.environ.get("PORT", "18790"))
    uvicorn.run("treg.api:app", host="0.0.0.0", port=port, reload="--reload" in sys.argv)


if __name__ == "__main__":
    main()
