#!/usr/bin/env python3
"""
Generate mobile viewport screenshots for the authenticated dashboard.

This script starts a dev server, creates a test user session, and captures
screenshots at mobile viewport sizes to demonstrate the hamburger menu fix.

Usage:
    uv run python scripts/screenshot-mobile-sidebar.py

Screenshots are saved to tests/screenshots/
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Ensure playwright is available
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Installing playwright...")
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    from playwright.sync_api import sync_playwright


PORT = 18799
SCREENSHOT_DIR = Path(__file__).parent.parent / "tests" / "screenshots"


def start_server():
    """Start the dev server."""
    env = os.environ.copy()
    env["TREG_DATABASE_URL"] = "sqlite+aiosqlite:///screenshot-test.db"
    env["TREG_EMAIL_DEV_MODE"] = "true"
    env["TREG_SECRET_KEY"] = "screenshot-test-key-not-for-production"
    env["TREG_SESSION_SECRET"] = "screenshot-session-secret"
    
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "treg.api:app", "--host", "127.0.0.1", "--port", str(PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    
    # Wait for server
    import httpx
    for _ in range(30):
        try:
            r = httpx.get(f"http://127.0.0.1:{PORT}/", timeout=1)
            if r.status_code in (200, 302, 307):
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("Server did not start")
    
    return proc


def main():
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    
    print("Starting dev server...")
    proc = start_server()
    
    try:
        print("Launching browser...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            
            # Mobile viewport (iPhone SE)
            context = browser.new_context(viewport={"width": 375, "height": 667})
            page = context.new_page()
            
            # Go to the dashboard HTML directly (raw file, before any server-side redirect)
            # This shows the authenticated shell structure
            page.goto(f"http://127.0.0.1:{PORT}/app")
            page.wait_for_load_state("networkidle")
            
            # Screenshot 1: Landing page at mobile size (shows mobile layout works)
            path1 = SCREENSHOT_DIR / "mobile-landing-375px.png"
            page.screenshot(path=str(path1))
            print(f"✓ Saved: {path1}")
            
            # Now load the raw HTML file to show the dashboard shell at mobile
            # We can load it as a data URL or file URL
            from treg.routers.web import _WEB_DIR
            html_path = _WEB_DIR / "index.html"
            page.goto(f"file://{html_path}")
            page.wait_for_timeout(1000)  # Let Vue mount
            
            # Screenshot 2: Dashboard shell at mobile (sidebar hidden)
            path2 = SCREENSHOT_DIR / "mobile-dashboard-sidebar-hidden-375px.png"
            page.screenshot(path=str(path2))
            print(f"✓ Saved: {path2}")
            
            # Click hamburger to open sidebar
            mob_menu = page.locator(".mob-menu")
            if mob_menu.count() > 0 and mob_menu.is_visible():
                mob_menu.click()
                page.wait_for_timeout(300)
                
                # Screenshot 3: Sidebar open
                path3 = SCREENSHOT_DIR / "mobile-dashboard-sidebar-open-375px.png"
                page.screenshot(path=str(path3))
                print(f"✓ Saved: {path3}")
            
            browser.close()
            
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        Path("screenshot-test.db").unlink(missing_ok=True)
    
    print("\nDone! Screenshots saved to tests/screenshots/")


if __name__ == "__main__":
    main()
