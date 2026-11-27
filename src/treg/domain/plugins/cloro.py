"""Cloro's request-dependent Bing Search price."""

from . import Request


def bing_search_units(request: Request) -> float:
    """Cloro charges in units of the five-credit base search, with two credits per add-on."""
    body = request.body if isinstance(request.body, dict) else {}
    try:
        pages = int(str(body.get("pages", 1)))
    except ValueError:
        pages = 10
    if not 1 <= pages <= 10:
        pages = 10
    include = body.get("include")
    overview = include.get("aioverview") if isinstance(include, dict) else None
    extra_pages = pages - 1
    has_overview = overview is True or isinstance(overview, dict)
    return (5 + 2 * extra_pages + 2 * has_overview) / 5
