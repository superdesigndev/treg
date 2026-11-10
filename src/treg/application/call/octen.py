"""Bound Octen platform holds and price this call's reported usage."""

from __future__ import annotations

import json
import math


RATE_KEYS = {
    "octen.web.search": frozenset({"call", "full_content_extra"}),
    "octen.web.search.broad": frozenset({"subquery", "full_content_extra"}),
    "octen.web.search.news": frozenset({"call", "full_content_extra"}),
    "octen.web.extract": frozenset({"standard", "advanced"}),
}


def rates_micro(endpoint_id: str, cost: dict) -> dict[str, int]:
    """The catalog owns the USD rates; reject incomplete or unsafe declarations."""
    rates = cost.get("octen_rates")
    if (not isinstance(rates, dict) or set(rates) != RATE_KEYS[endpoint_id]
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                   or round(v * 1_000_000) != v * 1_000_000 for v in rates.values())):
        raise ValueError("Octen catalog rates must be positive whole micro-USD values")
    return {name: round(value * 1_000_000) for name, value in rates.items()}


def _body(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _count(value: object, default: int, maximum: int) -> int:
    return value if type(value) is int and 1 <= value <= maximum else default


def _extras(options: dict, count: int) -> int:
    full = options.get("full_content")
    return max(0, count - 10) if isinstance(full, dict) and full.get("enable") is True else 0


def _news_count(request: dict) -> int:
    count = _count(request.get("count"), 5, 100)
    subjects = request.get("subjects")
    subjects = subjects if isinstance(subjects, dict) else {}
    if subjects.get("enable", True) is not False:
        count += _count(subjects.get("count"), 2, 5) * _count(
            subjects.get("max_sub_news"), 5, 20)
    return count


def estimate_micro(endpoint_id: str, rates: dict[str, int], body: bytes) -> int:
    request = _body(body)
    if endpoint_id == "octen.web.extract":
        urls = request.get("urls")
        count = len(urls) if isinstance(urls, list) and 1 <= len(urls) <= 20 else 20
        return count * rates["advanced"]  # auto and uncertain modes can resolve as advanced
    if endpoint_id == "octen.web.search.broad":
        queries = _count(request.get("max_queries"), 5, 30)
        options = request.get("search_options")
        options = options if isinstance(options, dict) else {}
        count = _count(options.get("count"), 5, 100)
        return queries * (rates["subquery"] + _extras(options, count) * rates["full_content_extra"])
    count = _news_count(request) if endpoint_id == "octen.web.search.news" else _count(
        request.get("count"), 5, 100)
    return rates["call"] + _extras(request, count) * rates["full_content_extra"]


def invalid_platform_parameter(endpoint_id: str, body: bytes) -> str | None:
    """Only platform calls need a strict spend ceiling; BYOK keeps the upstream's request."""
    try:
        request = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "body"
    if not isinstance(request, dict):
        return "body"
    if endpoint_id == "octen.web.extract":
        urls = request.get("urls")
        if not isinstance(urls, list) or not 1 <= len(urls) <= 20 \
                or any(not isinstance(url, str) or not url for url in urls):
            return "body.urls"
        if request.get("mode", "standard") not in ("standard", "advanced", "auto"):
            return "body.mode"
        return None
    if not isinstance(request.get("query"), str) or not request["query"].strip():
        return "body.query"
    if endpoint_id == "octen.web.search.broad":
        if type(request.get("max_queries", 5)) is not int or not 1 <= request.get("max_queries", 5) <= 30:
            return "body.max_queries"
        options = request.get("search_options", {})
        if not isinstance(options, dict):
            return "body.search_options"
        prefix = "body.search_options."
    else:
        options = request
        prefix = "body."
    if type(options.get("count", 5)) is not int or not 1 <= options.get("count", 5) <= 100:
        return prefix + "count"
    full = options.get("full_content", {})
    if not isinstance(full, dict) or ("enable" in full and type(full["enable"]) is not bool):
        return prefix + "full_content"
    if endpoint_id == "octen.web.search.news":
        subjects = request.get("subjects", {})
        if not isinstance(subjects, dict) or ("enable" in subjects and type(subjects["enable"]) is not bool):
            return "body.subjects"
        for name, default, maximum in (("count", 2, 5), ("max_sub_news", 5, 20)):
            value = subjects.get(name, default)
            if type(value) is not int or not 1 <= value <= maximum:
                return "body.subjects." + name
    return None


def observed_micro(endpoint_id: str, rates: dict[str, int], request: dict,
                   document: object, ceiling: int) -> int | None:
    """Missing or malformed usage falls back to the frozen hold, never to zero."""
    if not isinstance(document, dict):
        return None
    code = document.get("code")
    if type(code) is int and code != 0:
        return 0
    if code != 0:
        return None
    meta = document.get("meta")
    usage = meta.get("usage") if isinstance(meta, dict) else None
    if not isinstance(usage, dict):
        return None
    body = request.get("body") if isinstance(request, dict) else None
    body = body if isinstance(body, dict) else {}

    def integer(name: str) -> int | None:
        value = usage.get(name)
        return value if type(value) is int and value >= 0 else None

    if endpoint_id == "octen.web.extract":
        modes = usage.get("successful_by_mode")
        if not isinstance(modes, dict):
            return None
        standard, advanced = modes.get("standard_urls"), modes.get("advanced_urls")
        successful = integer("successful_urls")
        urls = body.get("urls")
        if any(type(v) is not int or v < 0 for v in (standard, advanced)) \
                or successful is None or standard + advanced != successful \
                or not isinstance(urls, list) or successful > len(urls):
            return None
        amount = standard * rates["standard"] + advanced * rates["advanced"]
    else:
        extra = integer("full_content_extra_count")
        if extra is None:
            return None
        if endpoint_id == "octen.web.search.broad":
            queries = integer("num_search_queries")
            maximum = _count(body.get("max_queries"), 5, 30)
            if queries is None or queries > maximum:
                return None
            amount = queries * rates["subquery"] + extra * rates["full_content_extra"]
        else:
            amount = rates["call"] + extra * rates["full_content_extra"]
    return amount if amount <= ceiling else None
