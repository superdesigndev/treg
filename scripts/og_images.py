"""Share images (og:image) for public pages: one 1200x630 JPEG per page, in the site's own look.

A page gets its own card when this script has written `src/treg/web/media/og/<path>.jpg`;
`og_image_url` in routers/web.py then serves it, and every other page keeps the site-wide
`/media/og.png`. Run it again after a page's headline or subtitle changes.

    uv run --frozen python scripts/og_images.py /compare/treg-vs-clay   # one page
    uv run --frozen python scripts/og_images.py --group compare         # one group
    uv run --frozen python scripts/og_images.py --all                   # every page it knows
    uv run --frozen python scripts/og_images.py --all --dry-run         # list, write nothing
    uv run --frozen python scripts/og_images.py --list                  # pages, groups, layouts

Layouts are the HTML templates below (`LAYOUTS`): "vs" (two products) and "page" (one headline).
A new look is one more template; a new group of pages is one more function in `GROUPS`.
Rendering uses the Playwright Chromium that `frontend/` installs (`npm --prefix frontend ci`),
and loads the site's Google fonts, so it needs the network.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

WEB = ROOT / "src" / "treg" / "web"
OUT = WEB / "media" / "og"

TREG_MARK = ('<svg viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#101010"/>'
             '<rect x="111" y="111" width="140.5" height="140.5" rx="20" fill="#fff"/>'
             '<rect x="260.5" y="260.5" width="140.5" height="140.5" rx="20" fill="#fff"/></svg>')

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@500;600'
         '&family=Inter:wght@400;500&family=Geist+Mono:wght@400&family=Geist+Pixel&display=block">')

BASE_CSS = """
* { box-sizing: border-box; margin: 0; }
html, body { width: 1200px; height: 630px; }
body { position: relative; overflow: hidden; background: #FDFCFB; color: #101010;
  font-family: Inter, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
.glow { position: absolute; inset: 0; z-index: 0;
  background: radial-gradient(38% 60% at 16% 30%, rgba(17,132,83,.18), transparent 70%),
              radial-gradient(38% 60% at 86% 26%, var(--them), transparent 70%),
              radial-gradient(60% 50% at 50% 110%, rgba(232,200,150,.22), transparent 70%); }
.frame { position: relative; z-index: 1; height: 100%; padding: 56px 72px; display: flex; flex-direction: column; }
.top { display: flex; align-items: center; gap: 14px; font: 600 26px "Inter Tight", Inter, sans-serif; }
.top svg { width: 40px; height: 40px; }
.top .path { margin-left: auto; font: 400 20px "Geist Mono", monospace; color: #8A8885; }
.mid { flex: 1; display: flex; flex-direction: column; justify-content: center; }
h1 { font-family: "Geist Pixel", "Geist Mono", monospace; font-weight: 400; letter-spacing: -.02em; line-height: 1.02; }
h1 .vs { color: #8A8885; font-size: .55em; margin: 0 .14em; vertical-align: .16em; }
.sub { margin-top: 22px; font-size: 30px; line-height: 1.35; color: #3F3D3A; max-width: 980px; }
.foot { display: flex; gap: 12px; align-items: center; font: 400 20px "Geist Mono", monospace; color: #6B6966; }
.foot b { color: #118453; font-weight: 400; }
.tile { width: 112px; height: 112px; border-radius: 28px; background: #fff; border: 1px solid #E8E5DF;
  box-shadow: 0 18px 40px -24px rgba(20,18,12,.35); display: grid; place-items: center; overflow: hidden; flex: none; }
.tile img, .tile svg { width: 100%; height: 100%; object-fit: contain; }
.tile.pad img { width: 72%; height: 72%; }
.logos { display: flex; align-items: center; gap: 22px; margin-bottom: 34px; }
.medal { width: 64px; height: 64px; border-radius: 50%; background: #101010; color: #fff; display: grid;
  place-items: center; font: 400 24px "Geist Pixel", monospace; }
"""


def _logo_uri(web_path: str) -> str:
    """A logo the page serves at `/media/...` or `/logos/...`, inlined as a data URI."""
    rel = web_path.removeprefix("/")
    f = WEB / rel
    mime = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[f.suffix[1:]]
    return f"data:{mime};base64," + base64.b64encode(f.read_bytes()).decode()


def _vs(card: dict) -> str:
    them = (f'<div class="tile{"" if card["logo_fills"] else " pad"}"><img src="{_logo_uri(card["logo"])}"/></div>'
            if card.get("logo") else "")
    size = 112 if len(card["name"]) <= 10 else 92 if len(card["name"]) <= 16 else 76
    return f"""<div class="glow"></div><div class="frame">
  <div class="top">{TREG_MARK}treg<span class="path">{html.escape(card["path_label"])}</span></div>
  <div class="mid">
    <div class="logos"><div class="tile">{TREG_MARK}</div><div class="medal">vs</div>{them}</div>
    <h1 style="font-size:{size}px">treg<span class="vs">vs</span>{html.escape(card["name"])}</h1>
    <p class="sub">{html.escape(card["sub"])}</p>
  </div>
  <div class="foot"><b>●</b> 0% markup · one key for paid tools · treg.to</div>
</div>"""


def _page(card: dict) -> str:
    return f"""<div class="glow"></div><div class="frame">
  <div class="top">{TREG_MARK}treg<span class="path">{html.escape(card["path_label"])}</span></div>
  <div class="mid">
    <h1 style="font-size:{card.get("size", 96)}px">{html.escape(card["title"])}</h1>
    <p class="sub">{html.escape(card["sub"])}</p>
  </div>
  <div class="foot"><b>●</b> 0% markup · one key for paid tools · treg.to</div>
</div>"""


LAYOUTS = {"vs": _vs, "page": _page}


def _compare_cards() -> list[dict]:
    from treg import compare_pages as cp
    cards = [{"path": "/compare", "layout": "page", "title": "How treg compares", "size": 104,
              "sub": "treg is like OpenRouter for tools. Honest, side-by-side comparisons with the other "
                     "ways an agent gets paid tools and data.",
              "them": "rgba(99,102,241,.14)", "path_label": "treg.to/compare"}]
    for key, c in cp.COMPETITORS.items():
        slug = cp.page_slug(key)
        cards.append({"path": f"/compare/{slug}", "layout": "vs", "name": cp.short(key), "sub": c["sub"],
                      "logo": c.get("logo"), "logo_fills": c.get("logo_fills", True),
                      "them": _soft(c.get("mono", "#6366F1")), "path_label": f"treg.to/compare/{slug}"})
    return cards


def _soft(hex_color: str, alpha: float = .16) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _launch_cards() -> list[dict]:
    return [{"path": "/aigc", "layout": "page", "title": "AI video for your agent", "size": 96,
             "sub": "Seedance, Veo, Wan, Hailuo and Kling with one token. Pay per finished clip.",
             "them": "rgba(17,132,83,.14)", "path_label": "treg.to/aigc"}]


# Each group lists its pages; add a function here to give another set of pages its own cards.
GROUPS = {"compare": _compare_cards, "launches": _launch_cards}


def out_file(path: str) -> Path:
    return OUT / ((path.strip("/") or "index") + ".jpg")


def _html(card: dict) -> str:
    body = LAYOUTS[card["layout"]](card)
    return (f'<!doctype html><html><head><meta charset="utf-8">{FONTS}<style>{BASE_CSS}'
            f':root{{--them:{card.get("them", "rgba(99,102,241,.14)")}}}</style></head><body>{body}</body></html>')


_RENDER_JS = r"""
const { chromium } = require(process.argv[1]);
const jobs = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  for (const j of jobs) {
    await page.goto('file://' + j.html, { waitUntil: 'networkidle' });
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: j.out, type: 'jpeg', quality: 88 });
    console.log('wrote ' + j.rel);
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def render(cards: list[dict]) -> None:
    pw = ROOT / "frontend" / "node_modules" / "playwright"
    if not pw.exists():
        sys.exit("Playwright is missing: run `npm --prefix frontend ci` first.")
    with tempfile.TemporaryDirectory() as tmp:
        jobs = []
        for i, card in enumerate(cards):
            page = Path(tmp) / f"{i}.html"
            page.write_text(_html(card), encoding="utf-8")
            out = out_file(card["path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            jobs.append({"html": str(page), "out": str(out), "rel": str(out.relative_to(ROOT))})
        spec = Path(tmp) / "jobs.json"
        spec.write_text(json.dumps(jobs), encoding="utf-8")
        subprocess.run(["node", "-e", _RENDER_JS, str(pw), str(spec)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="page paths, e.g. /compare/treg-vs-clay")
    ap.add_argument("--group", action="append", choices=sorted(GROUPS), help="every page in a group")
    ap.add_argument("--all", action="store_true", help="every page in every group")
    ap.add_argument("--dry-run", action="store_true", help="list what would be written")
    ap.add_argument("--list", action="store_true", help="list groups, layouts and pages")
    a = ap.parse_args()

    known = {g: fn() for g, fn in GROUPS.items()}
    if a.list:
        print("layouts:", ", ".join(LAYOUTS))
        for g, cards in known.items():
            print(f"{g}:", *(c["path"] for c in cards), sep="\n  ")
        return
    groups = list(GROUPS) if a.all else (a.group or [])
    picked = [c for g in groups for c in known[g]]
    by_path = {c["path"]: c for cards in known.values() for c in cards}
    for p in a.paths:
        if p not in by_path:
            sys.exit(f"{p}: no group lists this page. Add it to a group in GROUPS first.")
        picked.append(by_path[p])
    if not picked:
        ap.error("name a page, --group or --all")
    picked = list({c["path"]: c for c in picked}.values())
    if a.dry_run:
        for c in picked:
            print(f'{c["path"]} -> {out_file(c["path"]).relative_to(ROOT)} ({c["layout"]})')
        return
    render(picked)


if __name__ == "__main__":
    main()
