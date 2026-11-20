"""Pure path classification shared by call routers and exception adapters."""

from __future__ import annotations


_CALL_SURFACES = (
    ("/catalog/call/", "catalog_call"),
    ("/call/", "call"),
    ("/table/", "table"),
)


def split_call_path(path: str) -> tuple[str, str] | None:
    """Return the ingress surface and undecorated call path, if this is a call request."""
    for prefix, surface in _CALL_SURFACES:
        if path.startswith(prefix):
            return surface, path[len(prefix):]
    return None
