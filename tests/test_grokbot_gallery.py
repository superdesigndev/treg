from pathlib import Path


PAGE = Path(__file__).parents[1] / "src" / "treg" / "web" / "grokbot.html"

BOTS = {
    "ICP Map Coach": "https://x.ai/bot/yrm2MJ2nInUhoneTBSwJF",
    "Lookalike Scout": "https://x.ai/bot/mfaurGq6eY9rIvIpMfUFI",
    "Rival Watch Desk": "https://x.ai/bot/WKRY_T1y-KOmOn2q5vpRW",
    "SERP Watch Team": "https://x.ai/bot/iN9VkE6H4f4CLidzMaNaZ",
    "Creator Shortlist Crew": "https://x.ai/bot/6IU2bm7uuSPk6ETC-gC4D",
    "GTM Expert": "https://x.ai/bot/PIGZO9iQIeWkbmE79Q7bT",
}


def test_grokbot_gallery_lists_the_six_treg_bots_once_in_workflow_order() -> None:
    html = PAGE.read_text()

    assert '<section class="section bot-gallery" id="bots"' in html
    assert '<a class="nav-jump" href="#bots">Bot gallery</a>' in html
    assert html.count('class="bot-card"') == len(BOTS)
    assert html.count('class="bot-visual"') == len(BOTS)
    assert html.count('class="bot-eyes"') == len(BOTS)
    assert html.count('class="bot-tags"') == len(BOTS)
    assert html.count('class="bot-tag"') == len(BOTS) * 3
    assert html.count('class="bot-by branded"') == len(BOTS)
    assert html.count('class="bot-open-logo"') == len(BOTS)
    assert 'class="bot-number"' not in html
    assert 'class="crew-line"' not in html
    assert "grid-template-columns: repeat(6" in html
    assert "grid-column: span 2" in html
    assert "visual.addEventListener('pointermove'" in html
    assert "--eye-x" in html and "--eye-y" in html and "--eye-rot" in html
    for face in ("face-icp", "face-scout", "face-rival", "face-serp", "face-creator", "face-gtm"):
        assert f'<span class="bot-face {face}">' in html
    positions = [html.index(f">{name}</h3>") for name in BOTS]
    assert positions == sorted(positions)
    for name, url in BOTS.items():
        assert html.count(f">{name}</h3>") == 1
        assert html.count(f'href="{url}"') == 1
        assert f'aria-label="Open {name} on Grok"' in html


def test_existing_grokbot_setup_and_plugin_ctas_stay_intact() -> None:
    html = PAGE.read_text()

    for cta_id in ("ctaNav", "ctaHero", "ctaFinal"):
        assert f'id="{cta_id}">Setup treg</a>' in html
    assert html.count('href="https://x.ai/bot/plugin/55647425"') == 3
