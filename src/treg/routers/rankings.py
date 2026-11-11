"""The public /rankings page: the tools agents call most through this registry.

The page is one bundled file (`web/rankings.html`) with the first board (most used tools and its
daily chart) written into it as JSON, so a crawler and a link preview read the headline numbers
without a second request. Every other board is fetched from `/rankings/data/{board}` when the
reader scrolls near it, as OpenRouter's rankings do. All of them come from
`application.rankings.boards`: the one row the `treg-worker rankings refresh` cron saves, which each
process keeps five minutes.
"""
import hashlib
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import rankings
from ..config import get_settings
from ..infra.db import get_session
from .web import _WEB_DIR, _strip_hosted

router = APIRouter()

# What the page carries itself, and the board name -> key of every board it loads later.
_FIRST = ("through", "has_change", "floors", "categories", "chart", "tools", "pending")
_LATER = {"endpoints": "endpoints", "trending": "trending", "categories": "by_category",
          "reliable": "reliable", "fastest": "fastest", "headtohead": "head_to_head",
          "aimodels": "ai_models"}


def _cached(body: bytes, request: Request, media_type: str) -> Response:
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    sent = {t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")}
    if etag in sent:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=media_type, headers=headers)


def _item_list(boards: dict, base: str) -> str:
    """Search engines' reading of the first board: this week's ten most used tools, in order,
    each with its page. Built from the saved boards on every request, so it always matches them."""
    top = (boards.get("tools") or {}).get("7d") or []
    if not top:
        return ""
    items = [{"@type": "ListItem", "position": i + 1, "name": r["label"],
              "url": f"{base}/tools/{quote(r['provider'], safe='')}"} for i, r in enumerate(top[:10])]
    body = json.dumps({"@context": "https://schema.org", "@type": "ItemList",
                       "name": "Most used tools by AI agents this week", "itemListOrder": "https://schema.org/ItemListOrderDescending",
                       "numberOfItems": len(items), "itemListElement": items}, separators=(",", ":"))
    return '<script type="application/ld+json">' + body.replace("</", "<\\/") + "</script>"


@router.get("/rankings", include_in_schema=False)
async def rankings_page(request: Request, db: AsyncSession = Depends(get_session)):
    """Most used tools, most called endpoints, trending, top by category, most reliable and
    fastest, through the newest complete UTC day. No team, key or money appears on it."""
    page = _WEB_DIR / "rankings.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="rankings.html not bundled")
    boards = await rankings.boards(db)
    data = json.dumps({k: boards.get(k) for k in _FIRST}, separators=(",", ":"))
    # Inside <script type="application/json">: no "</" may close the element early.
    data = data.replace("</", "<\\/")
    base = get_settings().public_url.rstrip("/")
    html = (_strip_hosted(page.read_text(encoding="utf-8")).replace("{BASE}", base)
            .replace("{ITEMLIST}", _item_list(boards, base)).replace("{DATA}", data))
    return _cached(html.encode("utf-8"), request, "text/html; charset=utf-8")


@router.get("/rankings/data/{board}", include_in_schema=False)
async def rankings_board(board: str, request: Request, db: AsyncSession = Depends(get_session)):
    """One board the page loads when the reader scrolls near it."""
    key = _LATER.get(board)
    if key is None:
        raise HTTPException(status_code=404, detail="no such board")
    boards = await rankings.boards(db)
    # A board newer than the saved row (the deploy came before the next cron run) is empty, not an error.
    body = json.dumps({key: boards.get(key, {} if key == "ai_models" else [])}, separators=(",", ":")).encode()
    return _cached(body, request, "application/json")
