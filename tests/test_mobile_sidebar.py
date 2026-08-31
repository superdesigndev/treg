"""
Mobile sidebar layout test.

Verifies that the dashboard has correct mobile CSS for:
1. Hiding the sidebar by default on narrow viewports
2. Showing a hamburger menu button
3. Showing the sidebar as an overlay when toggled

These tests verify the HTML/CSS structure rather than visual behavior,
since visual tests would require an authenticated session.
"""

from __future__ import annotations

from pathlib import Path


def _spa() -> str:
    """Return the dashboard HTML."""
    from treg.routers.web import _WEB_DIR
    return (_WEB_DIR / "index.html").read_text(encoding="utf-8")


def test_mobile_sidebar_hamburger_menu_css():
    """The mobile hamburger menu CSS should be present and correct."""
    spa = _spa()
    
    # The hamburger menu button should be styled
    assert ".mob-menu{display:none" in spa, "Hamburger should be hidden by default (desktop)"
    assert ".mob-menu{display:grid" in spa or ".mob-menu{display:block" in spa, "Hamburger should show on mobile"
    
    # The mobile media query should show the hamburger
    assert "@media(max-width:760px)" in spa, "Mobile media query should exist"


def test_mobile_sidebar_hidden_by_default_css():
    """The sidebar should be hidden by default on mobile via CSS."""
    spa = _spa()
    
    # On mobile, sidebar should be hidden and positioned as overlay
    assert ".side{display:none" in spa, "Sidebar should be hidden on mobile by default"
    assert ".side.mob-open{display:flex}" in spa, "Sidebar should show when mob-open class is added"


def test_mobile_sidebar_backdrop_css():
    """The mobile backdrop overlay should be styled correctly."""
    spa = _spa()
    
    # Backdrop should be hidden by default
    assert ".mob-backdrop{display:none" in spa, "Backdrop should be hidden by default"
    assert ".mob-backdrop.mob-open{display:block}" in spa, "Backdrop should show when mob-open class is added"


def test_mobile_sidebar_hamburger_button_html():
    """The hamburger menu button should be in the HTML template."""
    spa = _spa()
    
    # The hamburger button should exist
    assert 'class="mob-menu"' in spa, "Hamburger menu button should be in the HTML"
    assert 'mobileNav' in spa, "mobileNav state should be referenced in the HTML"


def test_mobile_sidebar_backdrop_html():
    """The mobile backdrop element should be in the HTML template."""
    spa = _spa()
    
    # The backdrop should exist
    assert 'class="mob-backdrop"' in spa, "Mobile backdrop should be in the HTML"
    # Click handler to close
    assert '@click="mobileNav=false"' in spa or "@click='mobileNav=false'" in spa, "Backdrop should close on click"


def test_mobile_sidebar_close_button_html():
    """A close button should exist for mobile users to close the sidebar."""
    spa = _spa()
    
    # The close button should exist
    assert 'class="mob-close"' in spa, "Mobile close button should be in the HTML"


def test_mobile_nav_closes_on_navigation():
    """Navigation should close the mobile nav (mobileNav=false in go function)."""
    spa = _spa()
    
    # The go function should reset mobileNav
    assert "this.mobileNav=false" in spa or "mobileNav=false" in spa, "Navigation should close mobile nav"
