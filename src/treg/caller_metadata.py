"""Normalize self-reported caller metadata shared by control and execution routes."""

import re

from fastapi import Request

from .client_identity import _norm_client


_META_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_MAX_BUDGET_DIMS = 3        # each declared dimension = one indexed lookup per call + a row per value
# The reserved value standing for "every value of this dimension". Safe forever because the tag
# value charset (`_META_VALUE_RE`) excludes `*`, so no caller can send a value that collides with it.
TAG_DEFAULT = "*"


def _client_of(request: Request | None) -> str:
    """The calling RUNTIME from X-Treg-Client — attribution, not authentication (anything holding
    the token can claim any name, so this informs the roster and never gates anything). An
    unknown-but-well-formed name is kept, so a new runtime shows up without a release."""
    return _norm_client(request.headers.get("X-Treg-Client", "") if request is not None else "")
