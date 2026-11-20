"""Web Arena input and result rules. No provider call or database access lives here."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

TASKS = {"search": "web.search", "news": "web.search.news", "papers": "web.search.publications",
         "youtube": "youtube.search.videos", "maps": "google.serp.maps",
         "fetch": "web.extract", "sitemap": "web.map"}
TERMINAL = {"completed", "cancelled", "interrupted"}
RETENTION_DAYS = 30


class WebArenaError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def input_for(task: str, value: str, query: str = "") -> dict:
    if task not in TASKS:
        raise WebArenaError("Choose a Web Arena task.")
    value = value.strip()
    if not value or len(value) > 500:
        raise WebArenaError("Enter an input of 1 to 500 characters.")
    if task in {"search", "news", "papers", "youtube", "maps"}:
        return {"q": value, **({"limit": 10} if task in {"search", "news", "papers"} else {})}
    # A bare address such as "apple.com" means its https site; other schemes stay rejected.
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise WebArenaError("Enter a public HTTP or HTTPS URL.")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise WebArenaError("Enter a public website URL.")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            raise WebArenaError("Enter a public website URL.")
    except ValueError:
        pass
    # The call runtime applies its own SSRF checks to every real upstream request.
    if task == "sitemap":
        query = query.strip()
        if len(query) > 500:
            raise WebArenaError("Enter a search phrase of at most 500 characters.")
        return {"url": value, "limit": 10, **({"q": query} if query else {})}
    return {"url": value}


def url_rows(rows: list, site: str, limit: int = 100) -> dict:
    """A format and scope check; never call this full site coverage."""
    site_host = (urlsplit(site).hostname or "").lower()
    unique: list[str] = []
    seen: set[str] = set()
    invalid = 0
    for row in rows[:limit]:
        value = row if isinstance(row, str) else next((row.get(k) for k in ("url", "link", "loc") if row.get(k)), None) if isinstance(row, dict) else None
        try:
            parsed = urlsplit(value or "")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.lower() != site_host:
                invalid += 1
                continue
            clean = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
        except ValueError:
            invalid += 1
            continue
        if clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return {"urls": unique, "unique_valid_urls": len(unique), "invalid_urls": invalid,
            "coverage_percent": None, "coverage_note": "A full site URL list is not available for this run."}


def result_items(task: str, output: dict) -> list:
    value = output.get("pages" if task == "fetch" else "videos" if task == "youtube"
                       else "places" if task == "maps" else "results")
    return value if isinstance(value, list) else [value] if isinstance(value, (dict, str)) else []


def fetch_text(output: dict) -> str:
    for page in result_items("fetch", output):
        if isinstance(page, str) and page.strip():
            return page[:100_000]
        if isinstance(page, dict):
            for key in ("markdown", "markdown_content", "text", "raw_content", "content", "full_content"):
                value = page.get(key)
                if isinstance(value, str) and value.strip():
                    return value[:100_000]
                if key == "markdown" and isinstance(value, dict):
                    data = value.get("data")
                    if isinstance(data, str) and data.strip():
                        return data[:100_000]
    return ""


def valid_result(task: str, output: dict, input_value: str) -> bool:
    if task == "fetch":
        return bool(fetch_text(output).strip())
    if task == "sitemap":
        return url_rows(result_items(task, output), input_value)["unique_valid_urls"] > 0
    return bool(result_items(task, output))
