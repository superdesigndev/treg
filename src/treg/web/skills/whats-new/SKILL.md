---
name: whats-new
description: Use when the user wants to know what people are posting about a topic right now or since a recent point in time - the last few hours, today, since a launch, since their last check - such as the best clips, tips, reactions, launches, drama or sentiment on X, TikTok, YouTube, Reddit, Hacker News or Product Hunt. Also for "catch me up on X", "what's the internet saying about X", "what did I miss", or keeping up with a live event.
metadata:
  summary: Scrolls X, TikTok, YouTube, Reddit, Hacker News and Product Hunt for a topic over the last hours, keeps what is new and worth seeing, and hands back the best posts grouped into stories, with links and the cost of the run.
---

# whats-new

Your agent scrolls so you do not have to. Answer one question: **what is worth seeing about this topic
from the last N hours** (default 24h), across platforms, ranked, with links.

Runs on treg, through its MCP tools or the `treg` CLI; either is enough. Over MCP, `treg catalog
search` is `catalog_search`, `treg catalog get` is `catalog_get`, `treg call` is `call`. With
neither, treg is not set up: {BASE}/llms.txt says how. Install only if the human asked you to;
otherwise tell them how and stop.

## The run

1. **Plan.** Read the topic and the window. Write 3-6 searches, each a few keywords worded the
   way people title posts ("just launched", "introducing", "is now live", "tips", "glitch").
   Never put time words ("today", "latest", "2026", "news") in a search: the window does that.
   Cover every spelling of the name ("GTA 6", "GTA VI", "GTA6") and add a qualifier when the name
   is a common word ("Cursor AI editor", not "Cursor"). Keep one plain question in mind to judge
   rows against ("which AI products launched in the last day and did people care?").
2. **Resolve.** Before searching, name the places the topic lives: official handles on X, 2-4
   subreddits, 2-3 TikTok hashtags. A launch topic also gets Product Hunt and Show HN.
3. **Scroll.** Call every platform below that fits, in parallel. Search the catalog by the job;
   the vendors are who answers today, not a list to stick to.
4. **Merge.** With the CLI, save each answer to a file and run the merge script at the end of
   this file (save it once as `whats_new.py`):
   `python3 whats_new.py --since 24h --seen ~/.whats-new/<topic-slug>.json x=x1.json tiktok=tt.json ...`.
   It finds the rows in any provider's answer, drops posts older than the window, duplicates and
   posts shown in an earlier run, and ranks the rest by engagement within the platform, absolute
   reach and freshness. Over MCP only, apply the same rules by hand to the rows you got.
5. **Judge.** Read the top 60 rows yourself; the score only sorts them. Then:
   - Group rows about the same thing into one **story**. A story on 3+ platforms leads; one seen
     on a single platform is said to be so.
   - Rank by what the row shows, not by how often a thing is mentioned: a first-hand "I tried it,
     here is what happened" or a number beats ten reposts; promotion, engagement bait, giveaways,
     token shills and "comment AI for my course" count for nothing. Drop them.
   - Lead with what moved in the window, not with what was already known before it.
   - Pick the clips worth watching: funny, surprising or useful, not merely viral.
   - For the 2-3 biggest posts, read the top comments (`<platform> post comments` in the
     catalog; a Reddit post's id from its link, as `t3_<id>`) and quote the best one with its
     score.
6. **Hand back** the format below.

## Platforms

| Platform | Search the catalog for | Keep it recent | Example vendors |
| --- | --- | --- | --- |
| X | "search x posts" (`treg.x.search.posts`) | put `since_time:<unix seconds> min_faves:<n>` inside `q`; start at 100 likes, lower for a niche topic | AnyAPI, TikHub |
| TikTok | "search tiktok videos" (`treg.tiktok.search.videos`) | none; `limit: 30` and let the merge drop old ones | TikHub, AnyAPI |
| YouTube | "search youtube videos" | TikHub's web search takes `order_by=today` / `this_week`; if it fails, the routed search and let the merge drop old ones | TikHub |
| Reddit | "subreddit posts" with `sort=top`, `timeframe=day` for each resolved subreddit | keyword search (`timeframe=day`) is loose and pulls in unrelated front-page threads; use it only for a narrow name | ScrapeCreators, TikHub |
| Hacker News | "search hacker news" | `sort: date`, `tags: show_hn` for launches | Crawl4AI |
| Product Hunt | "product hunt today's launches" | today only by design; rows carry no votes or dates, so read them yourself for the ones on topic instead of trusting their rank | Crawl4AI |
| Instagram, Threads | "search instagram posts", "search threads posts" | none; the merge drops old ones | ScrapeCreators |

A platform that misses or fails is skipped and named in the coverage line, never retried more
than once. A typical run is 15-30 calls and costs cents; read each call's charge and sum it.

## Since the last scroll

`--seen` keeps the links already shown for this topic. "What's new?" or a scheduled run uses the
same file, so the answer is only what appeared since. If nothing new cleared the bar, say so in one
line instead of padding.

## Hand back

```
**<Topic> - last <window>** · <N> posts from <platforms> · $<cost>, 0 API keys

**1. <Story in plain words>** - <one line: what happened and why people care>
<the strongest evidence: a quote, a number, a first-hand result>
<platforms it appeared on> · <best link>

**2. ...** (3-8 stories)

**Worth watching**
- <clip in a few words> - <why> · <link> (3-5 clips, the fun or useful ones)

**The mood:** <one or two lines on how people feel, with a quoted comment>

Coverage: x 11 · tiktok 7 · youtube 9 · reddit 0 (no posts in window) · 4 duplicates, 17 too old, 0 already seen
```

No section headers beyond these, no list of sources at the end: every claim carries its own link.

## The merge script

Standard library only. Each argument is `SOURCE=FILE`, the JSON a `treg call` printed;
`python3 whats_new.py --selftest` checks it.

```python
import json, math, re, sys, time
from datetime import datetime

A = {"text": "text title desc description selftext tagline name content",
     "url": "url share_url hn_url link permalink",
     "author": "authorUsername author.unique_id author.username author.name channel.name author username",
     "created": "createdUtc created_utc create_time createTime created_at createdAt created published_time publishedTimeText publishedAt",
     "views": "viewCount statistics.play_count play_count views number_of_views viewCountText",
     "likes": "likeCount statistics.digg_count digg_count ups score points votes likes like_count",
     "comments": "replyCount statistics.comment_count comment_count num_comments comments commentCount",
     "shares": "retweetCount statistics.share_count share_count reposts quoteCount"}
UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}

def first(r, f):
    for path in A[f].split():
        v = r
        for p in path.split("."):
            v = v.get(p) if isinstance(v, dict) else None
        if v not in (None, "", [], {}):
            return v

def num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r"([\d.,]+)\s*([kmb])?", str(v or "").lower())
    return float(m.group(1).replace(",", "") or 0) * {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2) or "", 1) if m else 0

def epoch(v, now):
    if isinstance(v, (int, float)) and not isinstance(v, bool) or str(v).isdigit():
        v = float(v)
        return v / 1000 if v > 1e12 else v
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", str(v).lower())
    if m:
        return now - int(m.group(1)) * UNIT[m.group(2)]
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

def rows(doc):
    best = []
    def walk(o):
        nonlocal best
        if isinstance(o, dict):
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            d = [x for x in o if isinstance(x, dict)]
            best = d if len(d) > len(best) else best
            for x in d: walk(x)
    walk(doc.get("output", doc) if isinstance(doc, dict) else doc)
    # TikTok wraps each video in aweme_info, Reddit listings each post in data.
    return [next((r[k] for k in ("aweme_info", "data") if isinstance(r.get(k), dict)), r) for r in best]

def item(src, r, now):
    url = str(first(r, "url") or "")
    if src == "reddit" and (r.get("permalink") or (r.get("subreddit") and r.get("id"))):
        url = r.get("permalink") or f"/r/{r['subreddit']}/comments/{r['id']}"  # the thread, not the image
    if url.startswith("/r/"):
        url = "https://www.reddit.com" + url
    if re.search(r"tiktok\.com|x\.com|twitter\.com", url):
        url = url.split("?")[0]
    if src == "youtube" and not url and r.get("video_id"):
        url = "https://www.youtube.com/watch?v=" + r["video_id"]
    a = first(r, "author")
    return {"source": src, "url": url.rstrip("/"), "text": " ".join(str(first(r, "text") or "").split()),
            "author": a if isinstance(a, str) else None, "created": epoch(first(r, "created"), now),
            **{k: num(first(r, k)) for k in ("views", "likes", "comments", "shares")}}

def rank(items, span, now):
    for it in items:
        parts = [(it["likes"], .45), (it["comments"], .3), (it["shares"], .25)]
        if it["views"]:
            parts = [(it["views"], .35)] + [(v, w * .65) for v, w in parts]
        it["eng"] = sum(math.log1p(v) * w for v, w in parts)
    engs = sorted(x["eng"] for x in items)
    top = engs[int(len(engs) * .9)] if engs else 0
    for src in {x["source"] for x in items}:
        group = sorted((x for x in items if x["source"] == src), key=lambda x: x["eng"])
        for i, it in enumerate(group):  # within its platform: 1k TikTok likes are not 1k HN points
            it["pct"] = (i + 1) / len(group) if group[-1]["eng"] else .5
    for it in items:  # plus absolute reach, or the best of a quiet platform outranks a viral clip
        fresh = .5 if it["created"] is None else max(0, 1 - (now - it["created"]) / span)
        reach = min(1, it["eng"] / top) if top else .5
        it["score"] = round(100 * (.4 * it["pct"] + .4 * reach + .2 * fresh))
    out, urls, toks = [], set(), []
    for it in sorted(items, key=lambda x: -x["score"]):
        tk = set(re.findall(r"[a-z0-9]{3,}", it["text"].lower()))
        if (it["url"] and it["url"] in urls) or (len(tk) >= 6 and any(len(tk & o) / len(tk | o) > .7 for o in toks)):
            continue
        urls.add(it["url"]); toks.append(tk); out.append(it)
    return out

def short(v):
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.1f}k" if v >= 1e3 else f"{v:.0f}"

def main(argv):
    opt, pairs, it = {"--since": "24h", "--top": "60", "--seen": None}, [], iter(argv)
    for a in it:
        if a in opt: opt[a] = next(it)
        elif "=" in a: pairs.append(a.split("=", 1))
        else: sys.exit("usage: whats_new.py [--since 24h] [--top 60] [--seen FILE] SOURCE=FILE ...")
    m = re.fullmatch(r"(\d+)([hdw])", opt["--since"])
    if not m: sys.exit("--since takes 6h, 2d or 1w")
    span, now = int(m.group(1)) * {"h": 3600, "d": 86400, "w": 604800}[m.group(2)], time.time()
    items, old = [], 0
    for src, path in pairs:
        for r in rows(json.load(open(path))):
            x = item(src, r, now)
            if x["created"] and now - x["created"] > span: old += 1
            elif x["text"] or x["url"]: items.append(x)
    feed = rank(items, span, now)
    dupes, seen, shown_before = len(items) - len(feed), set(), 0
    if opt["--seen"]:
        try: seen = set(json.load(open(opt["--seen"])))
        except (OSError, ValueError): pass
        key = lambda x: x["url"] or x["text"][:80]
        shown_before = sum(key(x) in seen for x in feed)
        feed = [x for x in feed if key(x) not in seen]
        json.dump(sorted(seen | {key(x) for x in feed}), open(opt["--seen"], "w"))
    counts = {}
    for x in feed: counts[x["source"]] = counts.get(x["source"], 0) + 1
    print(f"# {len(feed)} posts in the last {opt['--since']} ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}); "
          f"dropped {old} older, {dupes} duplicates, {shown_before} already seen")
    for x in feed[:int(opt["--top"])]:
        age = "date?" if x["created"] is None else f"{(now - x['created']) / 3600:.0f}h"
        eng = ", ".join(f"{short(x[k])} {k}" for k in ("views", "likes", "comments", "shares") if x[k]) or "no engagement data"
        print(f"\n[{x['score']}] {x['source']} · {age}" + (f" · @{x['author']}" if x["author"] else "") + f" · {eng}")
        print(f"  {x['text'][:280]}" + (f"\n  {x['url']}" if x["url"] else ""))

def selftest():
    now = 1_800_000_000
    assert epoch("3 hours ago", now) == now - 10800 and epoch(1_700_000_000_000, now) == 1_700_000_000
    assert num("110,617 views") == 110617 and num("1.2K") == 1200
    assert rows({"output": {"videos": [{"aweme_info": {"desc": "a"}}]}}) == [{"desc": "a"}]
    assert item("reddit", {"subreddit": "x", "id": "1", "url": "https://i.redd.it/a.png"}, now)["url"].endswith("/r/x/comments/1")
    a = {"source": "x", "text": "same launch post here today friends", "url": "u1", "created": now,
         "author": None, "views": 0, "likes": 10, "comments": 0, "shares": 0}
    assert [i["url"] for i in rank([a, dict(a, url="u2", likes=1)], 86400, now)] == ["u1"]
    print("ok")

if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["--selftest"] else main(sys.argv[1:])
```
