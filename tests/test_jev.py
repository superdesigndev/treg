"""/jev: the landing page, its live launch-radar document, and the visitor judge endpoint.

The page is a bundled file; the board it renders comes from `/jev/xboost.json`, which serves the
worker's stored run or the bundled snapshot. The judge endpoint spends money on the demo team's
token, so its guards (configured, valid link, rate limit) are what these tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from treg.application import jev_xboost
from treg.config import get_settings

WEB = Path(__file__).parents[1] / "src" / "treg" / "web"


async def test_xboost_json_falls_back_to_the_bundled_snapshot(clients: AsyncClient):
    r = await clients.get("/jev/xboost.json")
    assert r.status_code == 200
    run = r.json()
    assert run["snapshot"] is True and run["live"] is False
    assert run["posts"] and {"organic", "paid", "irrelevant"} <= {jev_xboost.lane(p) for p in run["posts"]}
    assert run["manual"] == []


def test_the_bundled_demo_data_carries_no_real_email_addresses():
    """The signup demo is a synthetic sample; the lead demo replays a real run with every address replaced."""
    triage = json.loads((WEB / "media" / "jev" / "triage.json").read_text())
    assert triage["synthetic"] is True and len(triage["rows"]) >= 100
    assert {"enterprise", "smb", "influencer_affiliate", "fraud", "normal"} == {r["seg"] for r in triage["rows"]}
    fake_corp = {"northwind.io", "acmecloud.com", "lumen-labs.dev", "pikeandco.com", "brightloop.ai", "fernbank.co",
                 "harborsoft.com", "quillstack.io", "vantapoint.com", "oakridgedata.com", "meridianops.co",
                 "saltmarsh.dev", "tinderbox.ai", "cobaltcrm.com", "ridgelinehq.com", "glasswing.io",
                 "juniperbi.com", "ashgrove.co", "kestrel.dev", "larkspur.ai"}
    signals = json.loads((WEB / "media" / "jev" / "signals.json").read_text())
    for lead in signals["leads"]:
        email = (lead.get("contact") or {}).get("email")
        assert email is None or email.split("@")[1] in fake_corp, email


async def test_judge_refuses_when_the_live_demo_is_not_configured(clients: AsyncClient):
    r = await clients.post("/jev/xboost/judge", json={"url": "https://x.com/treg_ai/status/1234567890123"})
    assert r.status_code == 503


async def test_judge_validates_the_link_before_spending(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "jev_treg_token", "t", raising=False)
    monkeypatch.setattr(get_settings(), "ai_gateway_api_key", "k", raising=False)
    r = await clients.post("/jev/xboost/judge", json={"url": "https://example.com/not-a-post"})
    assert r.status_code == 400
    assert "status" in r.json()["detail"]


async def test_judge_is_rate_limited_per_ip(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "jev_treg_token", "t", raising=False)
    monkeypatch.setattr(get_settings(), "ai_gateway_api_key", "k", raising=False)

    async def fake_judge(http, url):
        handle, post_id = jev_xboost.parse_post_url(url)
        return {"id": post_id, "authorUsername": handle, "relevance": "inspiring_launch", "distribution": "organic"}

    monkeypatch.setattr(jev_xboost, "judge_url", fake_judge)
    codes = []
    for i in range(6):
        r = await clients.post("/jev/xboost/judge", json={"url": f"https://x.com/treg_ai/status/100000000000{i}"})
        codes.append(r.status_code)
    assert codes == [200] * 5 + [429]
    run = (await clients.get("/jev/xboost.json")).json()
    assert [m["id"] for m in run["manual"]] == [f"100000000000{i}" for i in (4, 3, 2, 1, 0)]
    assert run["live"] is True and run["posts"], "the first visitor judge keeps the snapshot's board"


def test_forensics_and_lane_are_pure_arithmetic():
    post = {"id": "1", "viewCount": 100_000, "likeCount": 2_000, "retweetCount": 300, "replyCount": 100,
            "bookmarkCount": 150, "quoteCount": 20, "createdUtc": 1_000}
    replies = [{"createdUtc": 1_100, "text": "great 🔥"}, {"createdUtc": 5_000, "text": "How does the routing decide the provider?"}]
    f = jev_xboost.forensics(post, {"followers": 50_000}, replies)
    assert f["rates"]["likes_per_view"] == 0.02 and f["views_per_follower"] == 2.0
    assert f["replies_sample"] == {"n": 2, "quick_share": 0.5, "short_share": 0.5, "generic_share": 0.5}
    assert jev_xboost.lane({"relevance": "inspiring_launch", "distribution": "paid_promotion"}) == "paid"
    assert jev_xboost.lane({"relevance": "inspiring_launch", "distribution": "artificial_engagement"}) == "organic"
    assert jev_xboost.lane({"relevance": "not_a_launch", "distribution": "organic"}) == "irrelevant"


def test_parse_post_url_accepts_x_and_twitter_links_only():
    assert jev_xboost.parse_post_url("https://twitter.com/jack/status/20?s=1") == ("jack", "20")
    assert jev_xboost.parse_post_url(" https://x.com/jack/status/20 ") == ("jack", "20")
    with pytest.raises(jev_xboost.XboostError):
        jev_xboost.parse_post_url("https://x.com/jack")


def test_with_manual_dedupes_and_caps():
    run = {"manual": [{"id": str(i)} for i in range(jev_xboost.MANUAL_CAP)]}
    out = jev_xboost.with_manual(run, {"id": "3"})
    assert out["manual"][0]["id"] == "3" and len(out["manual"]) == jev_xboost.MANUAL_CAP
    assert [m["id"] for m in out["manual"]].count("3") == 1


def test_tikhub_tweet_detail_becomes_a_search_row():
    data = {"text": "We just launched", "created_at": "Fri Sep 18 15:17:54 +0000 2026", "views": "2437013", "likes": 1034,
            "retweets": 220, "replies": 173, "quotes": 181, "bookmarks": 428,
            "author": {"name": "X Q", "screen_name": "quxiaoyin", "blue_verified": True, "sub_count": 37315},
            "entities": {"media": [{"media_url_https": "https://pbs.twimg.com/a.jpg"}]}}
    p = jev_xboost._post_from_detail("2100967557314547943", data)
    assert p["viewCount"] == 2437013 and p["likeCount"] == 1034 and p["authorUsername"] == "quxiaoyin"
    assert p["createdUtc"] == 1789744674 and p["media"][0]["url"].endswith("a.jpg")
    assert p["url"] == "https://x.com/quxiaoyin/status/2100967557314547943"


async def test_judge_answers_a_known_post_from_the_store_without_spending(clients: AsyncClient, monkeypatch):
    """A post already on the board (daily run or an earlier visitor) is not fetched or billed again."""
    monkeypatch.setattr(get_settings(), "jev_treg_token", "t", raising=False)
    monkeypatch.setattr(get_settings(), "ai_gateway_api_key", "k", raising=False)
    calls = []

    async def fake_judge(http, url):
        calls.append(url)
        return {"id": "5555", "authorUsername": "treg_ai", "relevance": "inspiring_launch", "distribution": "organic"}

    monkeypatch.setattr(jev_xboost, "judge_url", fake_judge)
    first = await clients.post("/jev/xboost/judge", json={"url": "https://x.com/treg_ai/status/5555"})
    again = await clients.post("/jev/xboost/judge", json={"url": "https://twitter.com/treg_ai/status/5555"})
    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["cached"] is True and "cached" not in first.json()
    assert calls == ["https://x.com/treg_ai/status/5555"], "the second request must not judge again"
    run = (await clients.get("/jev/xboost.json")).json()
    assert [m["id"] for m in run["manual"]].count("5555") == 1
    # a post from the daily snapshot is known too
    seed_id = run["posts"][0]["id"]
    r = await clients.post("/jev/xboost/judge", json={"url": f"https://x.com/someone/status/{seed_id}"})
    assert r.status_code == 200 and r.json()["cached"] is True and len(calls) == 1


def test_generic_reply_check_is_linear_and_keeps_its_verdicts():
    """The old anchored pattern backtracked exponentially on a long near-match (CodeQL py/redos);
    replies are strangers' text, reachable through the public judge endpoint."""
    import time
    from treg.application.jev_xboost import _is_generic
    for yes in ("great", "@a @b nice!", "🔥🔥🔥", "So true. Facts!", "congrats ❤️", "  wow  "):
        assert _is_generic(yes), yes
    for no in ("", "great product, how does pricing work?", "this broke my build", "greatness", "@someone"[:0]):
        assert not _is_generic(no), no
    t0 = time.perf_counter()
    assert not _is_generic("great " * 5000 + "x")
    assert time.perf_counter() - t0 < 0.5


