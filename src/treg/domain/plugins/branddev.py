"""Context.dev: the outcome rules its rows name that the predicate cannot say."""

from __future__ import annotations

from ..catalog.routing.paths import wall_page
from . import Answer, Request

_FORMATS = ("markdown", "html", "images", "screenshot")


def scrape_miss(request: Request | None, answer: Answer) -> bool:
    """A scrape that brought back no page: no format carries `data` (a target that could not be
    read still answers 200, naming the reason in the requested format's `error_code`), or the
    Markdown is a captcha, bot check, login or cookie wall rather than the page (`wall_page`)."""
    body = answer.body if isinstance(answer.body, dict) else {}
    data = {name: part.get("data") if isinstance(part := body.get(name), dict) else None
            for name in _FORMATS}
    if all(value is None or (isinstance(value, (str, list, dict)) and not value)
           for value in data.values()):
        return True
    return isinstance(data["markdown"], str) and wall_page(data["markdown"])
