"""Run `python -m treg` for the parity tests, serving on the listening socket the harness passes
in TREG_TEST_LISTEN_FD (see `cli_go_live._listener`).

With TREG_TEST_CATALOG_PATCH (JSON) it also patches the catalog: `providers` maps a provider to the
base URL its calls go to, `async` maps an endpoint id to the async descriptor it carries. The parity
tests use it to point async tools at a scripted local upstream."""
from __future__ import annotations

import dataclasses
import json
import os
import sys


def _patch_catalog(patch: dict) -> None:
    from treg import oauth_providers
    from treg.domain.catalog import store

    for name, base_url in patch.get("providers", {}).items():
        oauth_providers.REGISTRY[name] = dataclasses.replace(oauth_providers.REGISTRY[name], base_url=base_url)
    catalog = store.load()
    for endpoint_id, descriptor in patch.get("async", {}).items():
        catalog.by_id[endpoint_id]["async"] = descriptor


def main() -> None:
    fd = int(os.environ.pop("TREG_TEST_LISTEN_FD"))
    patch = os.environ.pop("TREG_TEST_CATALOG_PATCH", None)
    if patch is not None:
        _patch_catalog(json.loads(patch))

    import uvicorn

    run = uvicorn.run

    def serve_on_inherited_socket(app, *, host, port, **options):
        run(app, fd=fd, **options)

    uvicorn.run = serve_on_inherited_socket
    sys.argv = sys.argv[:1]
    from treg.__main__ import main as serve
    serve()


if __name__ == "__main__":
    main()
