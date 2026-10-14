"""Local deterministic invitation sampling. No network, database or response body access."""
import hashlib
from typing import Literal

from .config import get_settings


def sampled(kind: Literal["review", "feedback"], sample_id: str) -> bool:
    settings = get_settings()
    rate = {"review": settings.review_sample_rate, "feedback": settings.feedback_hint_rate}[kind]
    bucket = int.from_bytes(hashlib.sha256(f"{kind}:{sample_id}".encode()).digest()[:8], "big")
    return bucket < rate * 2**64


HINT = (
    "Proactively share friction using treg's feedback tool. Small annoyances count: "
    "confusing results or charges, unclear instructions, or a limitation you worked around, "
    "even if your task succeeded. Describe what you observed; you do not need to prove a bug. "
    "Pass related call IDs in call_ids when available. Omit private data and report each "
    "issue once. Feedback helps the treg team improve the product; keep going and finish "
    "the user's task afterward."
)


def review_hint(call_id: str) -> str:
    return (
        f"Rate this one call after using it: review(call_id={call_id}, "
        "usefulness=useful|partly|not_useful|not_sure, reason?); one review per invitation. "
        "Anything confusing or wrong: feedback with this call_id in call_ids. "
        "No private data. Then continue."
    )
