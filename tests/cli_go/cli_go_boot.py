"""Run `python -m treg` with a test patch to its catalog, read from TREG_TEST_CATALOG_PATCH (JSON):
`providers` maps a provider to the base URL its calls go to, `async` maps an endpoint id to the async
descriptor it carries. The parity tests use it to point async tools at a scripted local upstream."""
from __future__ import annotations

import dataclasses
import json
import os
import sys


def main() -> None:
    from treg import oauth_providers
    from treg.domain.catalog import store

    patch = json.loads(os.environ.pop("TREG_TEST_CATALOG_PATCH"))
    for name, base_url in patch.get("providers", {}).items():
        oauth_providers.REGISTRY[name] = dataclasses.replace(oauth_providers.REGISTRY[name], base_url=base_url)
    catalog = store.load()
    for endpoint_id, descriptor in patch.get("async", {}).items():
        catalog.by_id[endpoint_id]["async"] = descriptor
    sys.argv = sys.argv[:1]
    from treg.__main__ import main as serve
    serve()


if __name__ == "__main__":
    main()
