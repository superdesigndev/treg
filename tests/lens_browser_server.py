"""Local-only browser harness. Invoked from an empty temporary working directory."""
from __future__ import annotations

import asyncio
import os

# Never inherit an operator's provider configuration into a synthetic browser test.
for key in list(os.environ):
    if key.startswith("TREG_PLATFORM_KEY_"):
        del os.environ[key]
for provider in ("EXA", "CLORO", "DATAFORSEO"):
    os.environ[f"TREG_PLATFORM_KEY_{provider}"] = f"SYNTHETIC-LENS-{provider}"
os.environ["TREG_PLATFORM_PROVIDERS"] = "exa,cloro,dataforseo"
os.environ["TREG_LENS_ENABLED"] = "true"
os.environ["TREG_PROXY_SSRF_CHECK"] = "false"

from treg.__main__ import _run_upgrade
from treg.application.call import service
from lens_upstream import LensUpstream

service.relay = LensUpstream()

if __name__ == "__main__":
    import uvicorn
    asyncio.run(_run_upgrade())
    from treg.api import app
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["TREG_E2E_PORT"]))
