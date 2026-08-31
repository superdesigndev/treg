"""HTTP cookie helpers shared by authentication routes."""

from fastapi import Request

from ..config import get_settings


def _is_https(request: Request) -> bool:
    # behind a reverse proxy (Render), TLS is terminated upstream and forwarded as http + X-Forwarded-Proto.
    return request.headers.get("x-forwarded-proto", "").lower() == "https" or request.url.scheme == "https"


OAUTH_RETURN_COOKIE = "treg_oauth_return"


def _remember_oauth_return(resp, request: Request) -> None:
    """Park where to come back to after the user signs in.

    A RELATIVE path, deliberately — never a full URL. A stored absolute URL would have to be
    validated against our own origin before being redirected to, and getting that check subtly wrong
    is how open redirects happen. A path cannot leave the site.

    Short-lived: this is a detour of seconds, and a stale one would silently hijack the next sign-in.
    """
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    resp.set_cookie(OAUTH_RETURN_COOKIE, target, httponly=True, samesite="lax",
                    secure=_is_https(request), max_age=600)


def _take_oauth_return(request: Request) -> str | None:
    """The parked destination, if it is one we actually park — else None.

    Only `/oauth/authorize` is honoured. Accepting any path would turn this cookie into a general
    "redirect me anywhere after login" primitive, which is a phishing aid rather than a feature.
    """
    # Starlette quotes a cookie value containing separators, and not every client strips the quotes
    # back off. Tolerating them here costs nothing; assuming they are absent cost a failing test and
    # would have cost a silently-dropped authorization in production.
    target = (request.cookies.get(OAUTH_RETURN_COOKIE) or "").strip('"')
    return target if target.startswith("/oauth/authorize?") else None


def _same_origin(request: Request) -> bool:
    """CSRF guard for cookie-authenticated mutations: the Origin header (when a browser sends one)
    must be this server itself. "Itself" is EITHER the configured public URL or the host the request
    actually arrived on — public_url alone would reject legitimate localhost/dev-box origins."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        return True  # non-browser clients (and some same-origin GETs) send no Origin
    if origin == get_settings().public_url.rstrip("/"):
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if origin == f"{'https' if _is_https(request) else 'http'}://{host}":
        return True
    # `Origin: null` is a browser telling us the submitting document has an OPAQUE origin, which it
    # does after certain redirect chains — a consent form reached by way of a sign-in bounce through
    # GitHub, for instance. It is not evidence of a cross-site request, and treating it as one made
    # the OAuth consent screen fail intermittently: refused on the attempt that went through
    # sign-in, accepted on the retry that did not.
    #
    # `Sec-Fetch-Site` is the right corroboration. It is set by the browser and cannot be written by
    # script, so a page on another site cannot forge `same-origin` — which is exactly what Origin was
    # being used to prove.
    if origin == "null" and request.headers.get("sec-fetch-site") in ("same-origin", "none"):
        return True
    return False
