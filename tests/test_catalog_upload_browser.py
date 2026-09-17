"""Run with the optional test group and an installed Playwright Chromium.

Render the real dashboard against synthetic HTTP responses; no account or provider is contacted.
"""
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from treg.domain.catalog import store

playwright = pytest.importorskip("playwright.sync_api")
WEB = Path(__file__).resolve().parents[1] / "src/treg/web"


def test_upload_drawer_uses_cli_and_blocks_json_run(tmp_path):
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Install Playwright Chromium to run dashboard rendering checks")
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        calls = []

        def serve(route):
            path = urlsplit(route.request.url).path
            if path.startswith("/call/"):
                calls.append(path)
                return route.fulfill(json={"remaining_credits": 0})
            if path == "/app":
                return route.fulfill(path=str(WEB / "index.html"), content_type="text/html")
            asset = WEB / path.lstrip("/")
            if asset.is_file():
                return route.fulfill(path=str(asset))
            if path == "/meta":
                return route.fulfill(json={})
            if path == "/auth/me":
                return route.fulfill(status=401, json={})
            return route.fulfill(json=[])

        page.route("**/*", serve)
        page.goto("http://treg.test/app?ref=upload-review")
        page.wait_for_function("document.querySelector('#app').__vue_app__")
        page.evaluate("""() => {
            window.vm = document.querySelector('#app').__vue_app__._container._vnode.component.proxy;
        }""")
        page.wait_for_function("vm.demo.signin")
        page.evaluate("""vm.cfg={active:'test-team', orgs:{'test-team':{
            token:'synthetic-token', name:'Test team', role:'owner', org_id:1}}};
            vm.demo.signin=false""")
        ep = store.load().by_id["facecheck.web.face.upload"]
        ep = {**ep, "call_template": store.call_template(ep)}
        page.evaluate("e => { vm.epTry=e; vm.epTryAccess={tier:'credential'}; }", ep)
        drawer = page.get_by_role("dialog").filter(has_text="Try “facecheck.web.face.upload”")
        playwright.expect(drawer.get_by_text("Upload a local file with the CLI")).to_be_visible()
        assert "--upload images=@/path/to/file" in drawer.inner_text()
        assert "--data" not in drawer.inner_text()
        assert drawer.get_by_role("button", name="Manual", exact=True).count() == 0
        page.evaluate("vm.runEpTry()")
        assert not calls
        page.screenshot(path=str(tmp_path / "upload-desktop.png"))
        page.set_viewport_size({"width": 320, "height": 812})
        playwright.expect(drawer.get_by_text("Upload a local file with the CLI")).to_be_visible()
        assert drawer.evaluate("el => el.scrollWidth <= el.clientWidth")
        page.screenshot(path=str(tmp_path / "upload-mobile.png"))

        # Returning to a normal JSON endpoint restores its tabs and actual manual request.
        info = store.load().by_id["facecheck.account.usage"]
        page.evaluate("e => { vm.epTry=e; vm.epTryTab='manual'; vm.epTryBody='{}'; }", info)
        info_drawer = page.get_by_role("dialog").filter(has_text="Try “facecheck.account.usage”")
        playwright.expect(info_drawer.get_by_role("button", name="Manual", exact=True)).to_be_visible()
        info_drawer.get_by_role("button", name="❯ Run", exact=True).click()
        page.wait_for_function("vm.epTryStatus === 200")
        assert calls == ["/call/facecheck.account.usage"]
        browser.close()
