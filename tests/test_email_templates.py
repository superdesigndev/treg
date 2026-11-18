"""Transactional mail keeps its fills in clients that rewrite inline CSS.

Gmail strips the `background` shorthand from mail it relays while keeping `background-color` and
`bgcolor`; the invite's white "Sign in & accept" label was then drawn on white. Every fill must be
spelled in the surviving forms, and the button's white label must sit on a cell that keeps one.
"""
from __future__ import annotations

import re

import pytest

from treg import email


@pytest.fixture
def sent(monkeypatch):
    out: list[str] = []

    async def capture(to, subject, html, text):
        out.append(html)
        return True

    monkeypatch.setattr(email, "_send", capture)
    return out


async def _every_template() -> None:
    await email.send_otp("a@example.com", "482913")
    await email.send_invite("a@example.com", "tom@example.com", "Team", "member", "inv_x", "tok")
    await email.send_invite("a@example.com", "tom@example.com", "Team", "member", "inv_x", "tok",
                            shared="the skill “slides”")
    await email.send_topup_receipt("a@example.com", "Team", 20_000_000, 23_000_000, bonus_micro=2_000_000)
    await email.send_topup_receipt("a@example.com", "Team", 20_000_000, 23_000_000, auto=True)
    for reason in ("authentication_required", "max_attempts", "no_card", "stripe_unconfirmed"):
        await email.send_autotopup_disabled("a@example.com", "Team", reason)


async def test_no_template_uses_the_background_shorthand(sent):
    await _every_template()
    assert sent
    for html in sent:
        assert not re.search(r"[;\"']\s*background\s*:", html), html


async def test_the_white_button_label_sits_on_a_cell_that_keeps_its_fill(sent):
    await email.send_invite("a@example.com", "tom@example.com", "Team", "member", "inv_x", "tok")
    (html,) = sent
    cell = re.search(r'<td bgcolor="(#[0-9a-f]{6})" style="background-color:(#[0-9a-f]{6})[^"]*">'
                     r'<a [^>]*background-color:(#[0-9a-f]{6})[^>]*color:#ffffff[^>]*>Sign in', html)
    assert cell and set(cell.groups()) == {email._INK}
