"""Shared public-demo call limiter policy."""

from sqlalchemy.ext.asyncio import AsyncSession

from ... import ratestore


# Per-IP limiter for /call with a PUBLIC-DEMO token (the landing page publishes one shared member
# token, so the per-user daily cap is meaningless there — thousands of strangers are one "user").
PUBLIC_DEMO_HIT_NS = "pubdemo_call"
PUBLIC_DEMO_RATE_MAX = 10      # calls per IP per window
PUBLIC_DEMO_RATE_WINDOW_S = 60


class PublicDemoLimitError(Exception):
    """The shared public-demo credential exhausted its per-IP window."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def enforce_public_demo_ip_cap(client_ip: str, db: AsyncSession) -> None:
    """Per-IP cap for a call made with a SHARED public credential — the published demo token or
    the sandbox live wire. Both are one identity for thousands of strangers, so meter by client IP
    rather than by user. Records the sweep + hit and reports exhaustion; caller commits before
    translating the result."""
    await ratestore.sweep(db, PUBLIC_DEMO_HIT_NS)
    allowed = await ratestore.rate_check(
        db, PUBLIC_DEMO_HIT_NS, [(client_ip, PUBLIC_DEMO_RATE_MAX)], PUBLIC_DEMO_RATE_WINDOW_S)
    if not allowed:
        raise PublicDemoLimitError(
            f"demo limit reached ({PUBLIC_DEMO_RATE_MAX} calls/min per IP) — try again in a minute")
