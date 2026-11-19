"""Transactional email via Resend — the two sends the registry needs:

  1. send_otp    — the 6-digit sign-in code (POST /auth/email/start)
  2. send_invite — a team invitation with its one-time code (POST /orgs/{id}/invites)

Best-effort by design: every send is wrapped so a mail outage can NEVER break sign-in or invite
creation (the code still exists server-side; the CLI/dashboard flows keep working). If no
`TREG_RESEND_API_KEY` is set, sends are skipped (logged), so local/dev without a key is fine.
"""
from __future__ import annotations

import httpx

from .config import get_settings

RESEND_URL = "https://api.resend.com/emails"

# Email chrome, in the product's light skin (DESIGN.md): pale canvas, a white card, charcoal ink and
# an inverse button; colour only for a warning; DM Mono only for codes and figures. Inline styles and
# solid colours only, because mail clients drop <style> and CSS variables. Fills are `bgcolor` on a
# table cell plus `background-color`, never the `background` shorthand: Gmail strips that shorthand
# from mail it relays, and a white button label was once left on white. The mark is the hosted PNG
# of the brand mark (mail clients do not render SVG); its alt text keeps the header readable when
# images are blocked.
_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "'DM Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace"
_INK, _MUTED, _BOX = "#1a1a1a", "#5f5f5f", "#f4f4f1"
_P = f"margin:0 0 18px;color:{_MUTED};font-size:14px;line-height:1.6"
_H = f"margin:0 0 6px;color:{_INK};font-size:18px;line-height:1.4;font-weight:600"
_B = f"color:{_INK};font-weight:600"
_FINE = f"margin:18px 0 0;color:{_MUTED};font-size:12.5px;line-height:1.6"
_CODE = f"font-family:{_MONO};color:{_INK}"
_T = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'


def _box(inner: str, style: str = "", fill: str = _BOX) -> str:
    """A filled, rounded panel that keeps its fill in every client (a table cell with bgcolor)."""
    return (f'<table {_T} width="100%"><tr><td bgcolor="{fill}" '
            f'style="background-color:{fill};border-radius:12px;{style}">{inner}</td></tr></table>')


def _button(url: str, label: str) -> str:
    """The inverse pill. Its fill is spelled twice, `bgcolor` on the cell and `background-color` on the
    link, the two forms mail clients keep, because the white label is unreadable without it."""
    return (f'<table {_T}><tr><td bgcolor="{_INK}" style="background-color:{_INK};border-radius:999px">'
            f'<a href="{url}" style="display:inline-block;background-color:{_INK};border:1px solid {_INK};'
            f'border-radius:999px;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;'
            f'font-family:{_SANS};padding:11px 23px">{label}</a></td></tr></table>')


def _who(name: str) -> str:
    """A person or address in bold ink. An address goes in our own mailto link in the same ink: left
    bare, Gmail links it itself in blue and underlines it over the bold."""
    v = _esc(name)
    if "@" in name:
        v = f'<a href="mailto:{v}" style="color:{_INK};text-decoration:none">{v}</a>'
    return f'<b style="{_B}">{v}</b>'


def _wrap(body: str) -> str:
    base = get_settings().public_url.rstrip("/")
    return (
        f'<table {_T} width="100%" bgcolor="{_BOX}" style="background-color:{_BOX}"><tr>'
        f'<td align="center" style="padding:36px 16px;font-family:{_SANS}">'
        f'<table {_T} width="100%" bgcolor="#ffffff" style="max-width:460px;background-color:#ffffff;'
        'border:1px solid #e6e6e2;border-radius:20px"><tr><td style="padding:22px 28px 28px;text-align:left">'
        f'<div style="font-size:15px;font-weight:600;color:{_INK};line-height:24px;margin-bottom:22px">'
        f'<img src="{_esc(base)}/media/brand/logo.png" width="24" height="24" alt="treg" '
        'style="display:inline-block;vertical-align:top;border:0;border-radius:6px;margin-right:8px">treg</div>'
        f'{body}</td></tr></table>'
        '<div style="max-width:460px;margin:14px auto 0;text-align:center;color:#6e6e6e;font-size:12px;line-height:1.6">'
        "You received this because someone used this address with treg. If that wasn't you, ignore it.</div>"
        '</td></tr></table>'
    )


async def _send(to: str, subject: str, html: str, text: str) -> bool:
    """POST one email to Resend. Returns True on 2xx; never raises."""
    s = get_settings()
    if not s.resend_api_key:
        print(f"[email] no TREG_RESEND_API_KEY — skipping send to {to} ({subject!r})")
        return False
    payload = {"from": s.email_from, "to": [to], "subject": subject, "html": html, "text": text}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {s.resend_api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if r.status_code >= 300:
            print(f"[email] Resend {r.status_code} sending to {to}: {r.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001 — mail must never break the calling flow
        print(f"[email] send to {to} failed: {e}")
        return False


async def send_otp(email: str, code: str, ttl_minutes: int = 10) -> bool:
    body = (
        f'<p style="{_H}">Your sign-in code</p>'
        f'<p style="{_P}">Enter this to sign in to treg. It expires in {ttl_minutes} minutes.</p>'
        + _box(f'<div style="{_CODE};font-size:32px;font-weight:500;letter-spacing:8px;text-align:center">{code}</div>',
               "padding:18px") +
        f'<p style="{_FINE}">Didn\'t try to sign in? You can safely ignore this email — no one can act without this code.</p>'
    )
    html = _wrap(body)
    text = f"Your treg sign-in code is: {code}\nIt expires in {ttl_minutes} minutes. If you didn't request it, ignore this email."
    return await _send(email, f"{code} is your treg sign-in code", html, text)


async def send_invite(email: str, inviter: str, org_name: str, role: str, code: str,
                      email_token: str, expires_at: str = "", link_base: str = "",
                      shared: str = "") -> bool:
    """`shared` = a human phrase for a share-born invite (e.g. 'the skill “slideshow”') — the email
    then leads with what was shared and its button lands on that page after sign-in."""
    s = get_settings()
    from urllib.parse import quote
    # link_base must come from trusted_public_base(), never a raw Host header: the link carries a
    # sign-in token, so a client-chosen host would receive it.
    base = (link_base or s.public_url).rstrip("/")
    # The link carries `email_token`, NOT `code`; /auth/invite-signin resolves it to the invitee's
    # address and prefills the login. Neither secret signs anyone in.
    url = f"{base}/auth/invite-signin?t={quote(email_token)}"
    exp = f" It expires on {expires_at[:10]}." if expires_at else ""
    headline = (f'{_who(inviter)} shared {_esc(shared)} with you — join <b style="{_B}">{_esc(org_name)}</b> to use it'
                if shared else f'{_who(inviter)} invited you to <b style="{_B}">{_esc(org_name)}</b>')
    body = (
        f'<p style="{_H}">{headline}</p>'
        f'<p style="{_P}">You\'ve been added as <b style="{_B}">{_esc(role)}</b> on treg — call the team\'s tools with <b style="{_B}">no API keys on your machine</b>.{exp}</p>'
        + _button(url, "Sign in &amp; accept →") +
        f'<p style="{_FINE};margin-bottom:8px">Sign in with {_who(email)} and the invite appears automatically. Prefer a code? Use this one:</p>'
        + _box(f'<div style="{_CODE};font-size:13px;word-break:break-all">{_esc(code)}</div>', "padding:12px")
    )
    html = _wrap(body)
    text = (
        (f"{inviter} shared {shared} with you on treg (team {org_name}, as {role}).\n" if shared
         else f"{inviter} invited you to {org_name} as {role} on treg.\n")
        + f"Sign in at {url} with {email} and the invite appears automatically.\n"
        f"Or accept with this one-time code: {code}.{exp}"
    )
    subject = (f"{inviter} shared {shared} with you on treg" if shared
               else f"{inviter} invited you to {org_name} on treg")
    return await _send(email, subject, html, text)


async def send_topup_receipt(email: str, org_name: str, amount_micro: int, balance_micro: int,
                             *, auto: bool = False, bonus_micro: int = 0) -> bool:
    """Balance was added. Stripe emails its own payment receipt (the tax document); THIS email is the
    one that says what the money became — how much call balance the team now has — which Stripe cannot
    know. An automatic top-up says so plainly: an unattended charge nobody was told about is how a
    chargeback starts."""
    added, left = _money(amount_micro), _money(balance_micro)
    how = "Auto top-up" if auto else "Top-up"
    # The bonus is named next to the charge so the receipt and the balance agree: "$100 + $10 bonus"
    # explains a balance that went up $110 on a $100 card charge, which otherwise reads as a mistake.
    bonus = f' <b style="{_B}">+ {_money(bonus_micro)} bonus</b>' if bonus_micro else ""
    lead = (f'Your balance dropped below your auto top-up threshold, so we charged your saved card '
            f'<b style="{_B}">{added}</b>{bonus}.' if auto
            else f'Thanks — <b style="{_B}">{added}</b>{bonus} has been added to your balance.')
    body = (
        f'<p style="{_H}">{how} confirmed</p>'
        f'<p style="{_P}">{lead}</p>'
        + _box(f'<div style="color:{_MUTED};font-size:12.5px">Team</div>'
               f'<div style="color:{_INK};font-size:14px;font-weight:600;margin-bottom:10px">{_esc(org_name)}</div>'
               f'<div style="color:{_MUTED};font-size:12.5px">Balance now</div>'
               f'<div style="font-family:{_MONO};color:{_INK};font-size:26px;font-weight:500">{left}</div>',
               "padding:16px") +
        f'<p style="{_FINE}">Run <code style="font-family:{_MONO};color:{_INK};background-color:{_BOX};border-radius:5px;padding:1px 6px">treg balance</code> to see what it\'s made of and where it goes.</p>'
    )
    subject = f"{added} added to your treg balance"
    bonus_txt = f" (+ {_money(bonus_micro)} bonus)" if bonus_micro else ""
    text = (f"{how} confirmed: {added}{bonus_txt} added to {org_name}'s treg balance.\n"
            f"Balance now: {left}. Run `treg balance` for the detail.")
    return await _send(email, subject, _wrap(body), text)


async def send_autotopup_disabled(email: str, org_name: str, reason: str,
                                  *, recovery_pi: bool = False) -> bool:
    """Auto top-up turned itself off. Urgent by nature: the balance will now run out and every agent
    call starts failing with a 402, so the email names the cause and the one action that fixes it."""
    why = {
        "authentication_required": "your bank asked for verification (3-D Secure), which can't be done "
                                   "while you're away from the keyboard",
        "max_attempts": "the card was declined too many times in a row",
        "stripe_unconfirmed": "we could not confirm whether Stripe collected the payment",
        "no_card": "Stripe can no longer charge the saved card",
        "payment_refunded": "a top-up payment was refunded and its unspent credit removed",
    }.get(reason.split(":")[0], f"of a payment problem ({_esc(reason)})")
    if reason == "authentication_required" and not recovery_pi:
        fix = ("The attempted auto top-up was cancelled. Make a manual top-up in the dashboard "
               "to verify with your bank. Auto top-up will turn back on after that payment succeeds.")
    elif recovery_pi:
        fix = ("Open the dashboard and complete the payment once; your bank will remember the "
               "verification for future charges.")
    elif reason == "stripe_unconfirmed":
        fix = "Check billing history and contact support before switching auto top-up back on."
    elif reason == "payment_refunded":
        fix = ("Nothing else was charged. Switch auto top-up back on in the dashboard when you want "
               "refills to resume.")
    elif reason == "no_card":
        fix = ("Nothing was charged. Make a top-up in the dashboard to save a card; auto top-up "
               "turns back on once it is saved.")
    else:
        fix = "Open the dashboard to update your card and switch auto top-up back on."
    body = (
        f'<p style="{_H}">Auto top-up is off</p>'
        f'<p style="{_P}">We stopped topping up '
        f'<b style="{_B}">{_esc(org_name)}</b> because {why}.</p>'
        f'<p style="{_P};color:#ba6603">Once the balance runs out, '
        'calls will start failing until it\'s funded.</p>'
        f'<p style="{_P};margin:0">{fix}</p>'
    )
    text = (f"Auto top-up for {org_name} is off because {reason}. Once the balance runs out, calls "
            f"will fail until it's funded. {fix}")
    return await _send(email, f"Action needed: auto top-up is off for {org_name}",
                       _wrap(body), text)


def _money(amount_micro: int) -> str:
    """micro-USD → a money string. Keeps four decimals under a dollar, because sub-cent amounts are
    normal here (a catalog call runs ~$0.0006) and two decimals would print a real charge as $0.00."""
    try:
        v = int(amount_micro) / 1_000_000
    except (TypeError, ValueError):
        return "-"
    sign, v = ("-" if v < 0 else ""), abs(v)
    return f"{sign}${v:,.2f}" if v >= 1 else f"{sign}${v:.4f}"


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
