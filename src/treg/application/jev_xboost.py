"""The /jev "xboost" demo: launch posts on X from the last 24h -> engagement forensics -> jev verdict.

treg is a CLIENT here, not the proxy: the pipeline calls treg's own public `/call/` API with a member
token (`jev_treg_token`), exactly as a visitor's agent would, so the receipt on the page is a real
bill. jev answers through the Vercel AI Gateway (`ai_gateway_api_key`). The daily run is
`treg-worker jev xboost`; a visitor can also judge one post by URL (`judge_url`). Both write the
same JSON document under Ephemeral (`jev`, `xboost`) which `/jev/xboost.json` serves.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import re
import time

import httpx

from ..config import get_settings
from .house_calls import HouseCalls

KV_NS, KV_KEY = "jev", "xboost"
KV_TTL_S = 400 * 86400          # the run is replaced daily; the TTL only sweeps a dead deployment
MANUAL_CAP = 30                 # visitor-judged posts kept, newest first
JEV_USD_PER_INPUT_TOKEN = 0.042 / 1e6   # jev list price ($0.042 per 1M input tokens, output free)
JEV_URL = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
DEFAULT_TOPICS = ["introducing", "launching today", "now available", "we just shipped", "open source",
                  "AI agent", "AI agents", "Claude Code", "Codex", "Cursor", "MCP server", "developer tool", "API"]

# ---------------------------------------------------------------------------------- the rubric
QUESTIONS = {
    "relevance": {
        "type": "choice",
        "instructions": {
            "question": "Is this a product launch post we should study?",
            "context": "We collect launch posts for AI and developer tools to learn how strong launches "
                       "are written and how they spread.",
            "read": "post.text is the post. author.bio says who they are.",
        },
        "criteria": {
            "inspiring_launch": {
                "what": "A maker announcing their own AI or developer product, feature, model, or open-source release",
                "signals": ["'introducing', 'launching', 'now available', 'we shipped', 'today we're releasing'",
                            "author is the company, a founder, or an engineer on it",
                            "states what it does, shows a demo, gives numbers or pricing"],
            },
            "launch_other": {
                "what": "A launch or announcement, but not by the maker, or not an AI / developer tool",
                "signals": ["news account reporting a release", "consumer app, fiction tool, crypto token, hardware"],
            },
            "not_a_launch": {
                "what": "Commentary, opinion, news, memes, tutorials, hiring, jokes",
                "signals": ["reacts to someone else's product", "no product being released"],
            },
        },
    },
    "launch_type": {
        "type": "choice",
        "instructions": {"question": "If this is a launch, what kind of thing is being launched?",
                         "read": "post.text and author.bio. Pick 'none' when it is not a launch."},
        "criteria": {
            "model": "an AI model or model family, weights, fine-tune, benchmark result",
            "agent": "an autonomous agent or agent product for end users",
            "dev_tool": "a coding tool, IDE feature, CLI, SDK, plugin, or skill for developers",
            "api_infra": "an API, inference engine, hosting, data or infrastructure service",
            "open_source": "an open-source repo or library release where the open-source aspect is the headline",
            "consumer_app": "a consumer or prosumer app not aimed at developers",
            "none": "not a launch",
        },
    },
    "distribution": {
        "type": "choice",
        "instructions": {
            "question": "How did this post get its reach?",
            "how_to_read_the_state": {
                "post": "text, media, whether it links out, and the raw counts X shows.",
                "rates": "each engagement count divided by views. Typical organic X posts: likes 0.3-4% of "
                         "views (lower the bigger the post), replies 0.02-0.5%, reposts 0.05-1.5%, bookmarks "
                         "0.03-1.5% and higher for how-to or list content, quotes 0.005-0.15%.",
                "views_per_follower": "views divided by the author's followers. Organic posts from mid-size "
                                      "accounts usually land 0.1-3x; a viral organic post can reach 10-50x but "
                                      "then carries strong reposts and replies. 20x+ with weak engagement rates "
                                      "is the shape of paid reach.",
                "replies_sample": "the first replies. quick_share is the share posted within 10 minutes of the "
                                  "post; short_share is the share under 6 words; generic_share is the share that "
                                  "is only praise or emoji with no reference to the post's content.",
                "author": "followers, following, total posts, verified. A verified badge means paid "
                          "subscription, which X rewards with reply ranking, not fake engagement.",
            },
            "caveat": "Counts are public. Promoted status is not visible, so infer from shape, and prefer "
                      "organic when the ratios are ordinary.",
        },
        "criteria": {
            "organic": {
                "what": "Reach earned through the feed and reposts",
                "signals": ["ordinary engagement rates", "replies that engage with the content",
                            "views in a normal multiple of followers, or very high views WITH high reposts and quotes"],
            },
            "paid_promotion": {
                "what": "Reach bought as an ad or a promoted post",
                "signals": ["views many times followers with likes under 0.3% of views",
                            "replies and bookmarks per view near zero",
                            "promotional copy: product launch, sign up, link in the post, discount"],
                "not_for": "a genuinely viral post, which has high views but also high reposts and replies",
            },
            "artificial_engagement": {
                "what": "Likes, reposts, or replies inflated by pods, purchased engagement, or bots",
                "signals": ["likes above 8% of views or reposts above 3% of views",
                            "replies that are mostly generic praise, very short, and posted within minutes",
                            "small account with engagement out of proportion to views"],
                "not_for": "a fan community replying enthusiastically to a celebrity or product account",
            },
        },
    },
    "is_organic": {
        "type": "boolean",
        "instructions": {"question": "Is this post's reach and engagement plausibly earned without payment or manipulation?",
                         "weigh_most": "engagement rates against the typical ranges, then views_per_follower, then replies_sample"},
        "criteria": {"true": "Ratios ordinary, replies substantive, reach proportionate to followers or explained by reposts",
                     "false": "A rate far outside the typical range with nothing in the content to explain it, "
                              "or a reply sample dominated by quick generic praise"},
    },
    "authenticity": {
        "type": "score",
        "instructions": "How authentic does this post's engagement look overall?",
        "criteria": [
            {"summary": "clearly manipulated", "signals": ["several rates far outside typical ranges", "bot-like reply sample"]},
            {"summary": "suspicious", "signals": ["one rate well outside range", "reply sample partly generic"]},
            {"summary": "probably organic", "signals": ["rates mostly ordinary", "one mild oddity explained by the content"]},
            {"summary": "clearly organic", "signals": ["ordinary rates", "substantive replies", "reach in proportion to followers or reposts"]},
        ],
    },
}

# A reply is "generic" when nothing is left once mentions, stock praise, the usual emoji and
# punctuation are taken out. Done by removal, not by one anchored pattern: the old
# `^(@\w+\s*)*((great|…|\s)+)$` nested a repeat over overlapping alternatives and backtracked
# exponentially on a long run that ALMOST matched, and this text is a stranger's reply on X.
_GENERIC_WORDS = re.compile(
    r"@\w+|\b(?:great|nice|amazing|awesome|love|wow|congrats|congratulations|this|so true|facts|fire)\b", re.I)
_GENERIC_NOISE = str.maketrans("", "", "🔥👏💯🙌❤\ufe0f!. \t\r\n")


def _is_generic(text: str) -> bool:
    text = (text or "").strip()[:500]   # a reply is short; the cap bounds the work on anything that is not
    return bool(text) and not _GENERIC_WORDS.sub("", text).translate(_GENERIC_NOISE)
_POST_URL = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{1,25})")


class XboostError(Exception):
    """A bounded, user-safe reason: shown on the page, never a vendor body."""


def configured() -> bool:
    s = get_settings()
    return bool(s.jev_treg_token and s.ai_gateway_api_key)


# --------------------------------------------------------------------------------- pure helpers
def forensics(p: dict, profile: dict | None, replies: list[dict]) -> dict:
    """Per-view engagement rates, views per follower and the shape of the first replies."""
    v = max(p.get("viewCount") or 0, 1)
    likes, rts = p.get("likeCount") or 0, p.get("retweetCount") or 0
    reps, bms, qts = p.get("replyCount") or 0, p.get("bookmarkCount") or 0, p.get("quoteCount") or 0
    fol = (profile or {}).get("followers") or 0
    sample = [r for r in replies if r.get("createdUtc")]
    quick = [r for r in sample if r["createdUtc"] - (p.get("createdUtc") or 0) <= 600]
    short = [r for r in sample if len(re.sub(r"@\w+", "", r.get("text") or "").split()) < 6]
    generic = [r for r in sample if _is_generic(r.get("text"))]
    share = lambda xs: round(len(xs) / len(sample), 2) if sample else None  # noqa: E731
    return {
        "rates": {"likes_per_view": round(likes / v, 5), "replies_per_view": round(reps / v, 5),
                  "reposts_per_view": round(rts / v, 5), "bookmarks_per_view": round(bms / v, 5),
                  "quotes_per_view": round(qts / v, 5)},
        "views_per_follower": round(v / fol, 2) if fol else None,
        "replies_sample": {"n": len(sample), "quick_share": share(quick), "short_share": share(short),
                           "generic_share": share(generic)},
    }


def lane(p: dict) -> str:
    """The kanban lane a judged post lands in. Kept in step with the page's LANE()."""
    if p.get("relevance") != "inspiring_launch":
        return "irrelevant"
    return "paid" if p.get("distribution") == "paid_promotion" else "organic"


def parse_post_url(url: str) -> tuple[str, str]:
    m = _POST_URL.match((url or "").strip())
    if not m:
        raise XboostError("paste a post link like https://x.com/handle/status/1234567890")
    return m.group(1), m.group(2)


def _state(p: dict, profile: dict, f: dict) -> dict:
    text = p.get("text") or ""
    return {
        "post": {"text": text[:800], "has_media": bool(p.get("media")),
                 "links_out": "http" in text or bool(re.search(r"\w\.\w{2,}/", text)),
                 "age_hours": round((time.time() - (p.get("createdUtc") or time.time())) / 3600, 1),
                 "counts": {k: p.get(k) for k in ("viewCount", "likeCount", "retweetCount", "replyCount",
                                                  "quoteCount", "bookmarkCount")}},
        **f,
        "author": {"handle": p.get("authorUsername"), "verified": p.get("authorVerified"),
                   "followers": profile.get("followers"), "following": profile.get("following"),
                   "total_posts": profile.get("tweets"), "bio": (profile.get("bio") or "")[:160]},
    }


def _trim(p: dict, profile: dict, replies: list[dict], f: dict, answers: dict, usage: dict, topic: str | None) -> dict:
    """What the page needs and nothing else: the stored document is one JSON row."""
    media = p.get("media") or []
    img = media[0].get("url") if media and isinstance(media[0], dict) else None
    return {
        "id": str(p.get("id")), "url": p.get("url") or f"https://x.com/{p.get('authorUsername')}/status/{p.get('id')}",
        "text": (p.get("text") or "")[:1000], "createdUtc": p.get("createdUtc"), "topic": topic,
        "authorName": p.get("authorName"), "authorUsername": p.get("authorUsername"),
        "authorVerified": bool(p.get("authorVerified")), "image": img,
        **{k: p.get(k) or 0 for k in ("viewCount", "likeCount", "retweetCount", "replyCount", "quoteCount", "bookmarkCount")},
        "profile": {k: profile.get(k) for k in ("avatarUrl", "followers", "following", "tweets", "bio", "verified")},
        "replies": [{"authorHandle": r.get("authorHandle"), "text": (r.get("text") or "")[:200],
                     "createdUtc": r.get("createdUtc")} for r in replies[:6]],
        "forensics": f,
        "relevance": answers["relevance"]["choice"], "relevance_probs": answers["relevance"]["probabilities"],
        "launch_type": answers["launch_type"]["choice"],
        "distribution": answers["distribution"]["choice"], "distribution_probs": answers["distribution"]["probabilities"],
        "is_organic": answers["is_organic"]["probability"],
        "authenticity": answers["authenticity"]["score"], "authenticity_probs": answers["authenticity"]["probabilities"],
        "jev_cost": usage["cost"], "jev_tokens": usage["input_tokens"],
    }


# ---------------------------------------------------------------------------------- the clients
class _Treg:
    """treg's public /call/ API as a visitor's agent sees it: routed endpoint, JSON body, real bill."""

    def __init__(self, http: httpx.AsyncClient):
        self.house = HouseCalls(http, get_settings().jev_treg_token, "jev-xboost")

    @property
    def cost_usd(self) -> float:
        return self.house.cost_micro / 1e6

    @property
    def calls(self) -> dict[str, int]:
        return self.house.calls

    async def call(self, endpoint: str, body: dict, kind: str, **headers: str) -> dict:
        return await self._request("POST", endpoint, kind, json=body, headers=headers)

    async def get(self, endpoint: str, params: dict, kind: str) -> dict:
        return await self._request("GET", endpoint, kind, params=params)

    async def _request(self, method: str, endpoint: str, kind: str, *, json: dict | None = None,
                       params: dict | None = None, headers: dict | None = None) -> dict:
        a = await self.house.request(method, endpoint, kind, json=json, params=params, headers=headers)
        if a.status == 0:
            raise XboostError(f"treg call failed: {a.error}")
        if a.status >= 400:
            if a.status in (404, 502):       # a routed miss: every child said no
                return {}
            raise XboostError(f"treg answered HTTP {a.status} on {endpoint}")
        return a.body


async def _jev(http: httpx.AsyncClient, state: dict) -> tuple[dict, dict]:
    """jev via the Vercel AI Gateway. Returns (answers, usage) with `cost` at jev's list price."""
    headers = {"Authorization": f"Bearer {get_settings().ai_gateway_api_key}", "Content-Type": "application/json",
               "ai-model-id": "typesafe-ai/jev", "ai-evaluation-model-specification-version": "4",
               "ai-gateway-protocol-version": "0.0.1"}
    last: dict = {}
    for i in range(4):
        try:
            r = await http.post(JEV_URL, headers=headers, json={"state": state, "questions": QUESTIONS}, timeout=60)
            last = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = {"error": type(exc).__name__}
        if isinstance(last, dict) and "answers" in last:
            break
        await asyncio.sleep(1.5 * (i + 1))
    else:
        raise XboostError("jev did not answer; try again in a minute")
    u = last.get("usage") or {}
    tokens = int(u.get("inputTokens") or 0)
    return last["answers"], {"input_tokens": tokens, "cost": round(tokens * JEV_USD_PER_INPUT_TOKEN, 8)}


def _profile(d: dict) -> dict:
    o, raw = d.get("output") or {}, d.get("raw") or {}
    rd = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw if isinstance(raw, dict) else {}
    av = rd.get("avatarUrl") or rd.get("profile_image_url") or rd.get("avatar")
    if isinstance(av, dict):
        av = av.get("image_url") or av.get("url")
    return {"avatarUrl": av if isinstance(av, str) else None, "bio": o.get("description") or rd.get("bio"),
            "name": o.get("name") or rd.get("displayName"), "followers": o.get("followers"),
            "following": o.get("following"), "tweets": o.get("posts_count"), "verified": o.get("is_verified")}


def _rows(d: dict, *keys: str) -> list[dict]:
    o = d.get("output")
    if isinstance(o, list):
        return [r for r in o if isinstance(r, dict)]
    if isinstance(o, dict):
        for k in keys:
            if isinstance(o.get(k), list):
                return [r for r in o[k] if isinstance(r, dict)]
    return []


async def _judge(http: httpx.AsyncClient, treg: _Treg, p: dict, topic: str | None,
                 profiles: dict[str, dict] | None = None) -> dict:
    handle = p.get("authorUsername") or ""
    if profiles is not None and handle in profiles:
        profile = profiles[handle]
    else:
        profile = _profile(await treg.call("treg.x.user.profile", {"username": handle}, "profile"))
        if profiles is not None:
            profiles[handle] = profile
    p.setdefault("authorName", profile.get("name") or handle)
    p.setdefault("authorVerified", profile.get("verified"))
    replies = _rows(await treg.call("treg.x.post.comments", {"tweet_id": str(p["id"]), "limit": 20}, "replies"),
                    "comments", "items")
    f = forensics(p, profile, replies)
    answers, usage = await _jev(http, _state(p, profile, f))
    return _trim(p, profile, replies, f, answers, usage, topic)


# ---------------------------------------------------------------------------------- entry points
async def run_daily(http: httpx.AsyncClient, *, topics: list[str] | None = None, max_posts: int = 60,
                    min_likes: int = 150) -> dict:
    """The scheduled run: search each phrase for posts under 24h old, keep the top by views, judge each."""
    if not configured():
        raise XboostError("jev_treg_token and ai_gateway_api_key are not both set")
    t0 = time.perf_counter()
    treg = _Treg(http)
    topics = topics or DEFAULT_TOPICS
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    posts: dict[str, dict] = {}
    now = time.time()
    for t in topics:
        q = f'"{t}"' if " " in t else t
        d = await treg.call("treg.x.search.posts", {"q": f"{q} min_faves:{min_likes} since:{since} -filter:replies", "limit": 20},
                            "search")
        for r in _rows(d, "posts", "items"):
            if r.get("id") and now - (r.get("createdUtc") or 0) < 86400:
                posts.setdefault(str(r["id"]), {**r, "topic": t})
    ranked = sorted(posts.values(), key=lambda p: -(p.get("viewCount") or 0))[:max_posts]

    sem = asyncio.Semaphore(6)
    profiles: dict[str, dict] = {}

    async def one(p: dict) -> dict | None:
        async with sem:
            try:
                return await _judge(http, treg, p, p.get("topic"), profiles)
            except XboostError:
                return None

    judged = [j for j in await asyncio.gather(*(one(p) for p in ranked)) if j]
    return {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "since": since, "topics": topics,
        "seconds": round(time.perf_counter() - t0, 1), "posts_found": len(posts), "calls": treg.calls,
        "costs": {"treg": round(treg.cost_usd, 5), "jev": round(sum(j["jev_cost"] for j in judged), 6)},
        "posts": judged, "manual": [],
    }


_DETAIL = "tikhub.x.twitter-web-fetch-tweet-detail"   # the post by id: counts, text, author, media


def _post_from_detail(post_id: str, data: dict) -> dict:
    """TikHub's tweet-detail row in the search-row shape the pipeline reads."""
    author = data.get("author") or {}
    try:
        created = int(datetime.strptime(data["created_at"], "%a %b %d %H:%M:%S %z %Y").timestamp())
    except (KeyError, ValueError, TypeError):
        created = int(time.time())
    media = [{"url": m.get("media_url_https")} for m in ((data.get("entities") or {}).get("media") or [])
             if isinstance(m, dict) and m.get("media_url_https")]
    counts = {k: int(data.get(src) or 0) for k, src in (("viewCount", "views"), ("likeCount", "likes"), ("retweetCount", "retweets"),
                                                         ("replyCount", "replies"), ("quoteCount", "quotes"), ("bookmarkCount", "bookmarks"))}
    return {"id": post_id, "text": data.get("text") or data.get("display_text") or "", "createdUtc": created,
            "url": f"https://x.com/{author.get('screen_name') or 'i'}/status/{post_id}", "media": media,
            "authorUsername": author.get("screen_name"), "authorName": author.get("name"),
            "authorVerified": bool(author.get("blue_verified")), **counts}


async def _fetch_post(treg: _Treg, post_id: str) -> dict | None:
    d = await treg.get(_DETAIL, {"tweet_id": post_id}, "post")
    data = d.get("data") if isinstance(d.get("data"), dict) else None
    if not data or not data.get("author"):
        return None
    return _post_from_detail(post_id, data)


async def judge_url(http: httpx.AsyncClient, url: str) -> dict:
    """A visitor's post: fetch it by id, then run the same forensics + jev."""
    if not configured():
        raise XboostError("the live demo is not configured on this server")
    handle, post_id = parse_post_url(url)
    treg = _Treg(http)
    p = await _fetch_post(treg, post_id)
    if p is None:
        raise XboostError(f"could not read that post; it may be deleted, protected, or @{handle} may be wrong")
    p.setdefault("authorUsername", handle)
    j = await _judge(http, treg, p, None)
    j["manual"] = True
    j["treg_cost"] = round(treg.cost_usd, 5)
    j["judged_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return j


def with_manual(run: dict, judged: dict) -> dict:
    """Prepend a visitor's verdict, replacing an earlier verdict on the same post, capped."""
    manual = [m for m in run.get("manual") or [] if m.get("id") != judged["id"]]
    return {**run, "manual": [judged, *manual][:MANUAL_CAP]}
