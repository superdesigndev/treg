"""The fixed upstream hosts of the catalog's providers."""

from __future__ import annotations

from urllib.parse import urlsplit

from .. import oauth_providers
from ..domain.catalog import store as catalog_store


def catalog_hosts() -> frozenset[str]:
    """Every host a catalog call can reach. Each comes from a provider definition or an approved
    catalog target, never from a caller, so connect-time pinning leaves these on httpx's own path
    (infra/upstream/pinned.py). A host built from caller or connection input must not be listed."""
    hosts: set[str] = set()
    for provider in oauth_providers.REGISTRY.values():
        urls = [provider.base_url, *(t.base_url for t in provider.catalog_targets),
                *(str(x.get("base_url") or "") for x in provider.extra_tools)]
        for url in urls:
            host = (urlsplit(url).hostname or "").lower() if url else ""
            if host:
                hosts.add(host)
    for endpoint in catalog_store.load().by_id.values():
        if endpoint.get("host"):
            hosts.add(str(endpoint["host"]).lower())
    return frozenset(hosts)
