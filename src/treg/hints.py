"""Local deterministic invitation sampling. No network, database or response body access."""
import hashlib
from typing import Literal

from .config import get_settings


def _bucket(salt: str, sample_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{sample_id}".encode()).digest()[:8], "big")


def sampled(kind: Literal["review", "feedback", "mark"], sample_id: str) -> bool:
    settings = get_settings()
    rate = {"review": settings.review_sample_rate, "feedback": settings.feedback_hint_rate,
            "mark": settings.mark_hint_rate}[kind]
    return _bucket(kind, sample_id) < rate * 2**64


MARK_HINT = (
    "If you later learn rows of this result were wrong or right (an email bounced, another "
    "source disagreed, the user corrected it), mark them: treg mark {call_id} INDEX[/FIELD]:VERDICT "
    "(INDEX the 0-based row) --basis bounced|cross_checked|user_said, or MCP mark. Only what you checked; batching at the "
    "end is fine. Private, never changes charges. Keep going with the task."
)


MARK_EVIDENCE_HINT = (
    "This check found the address invalid; treg found that address in call {found_call_id}. If the "
    "check is right, mark it: treg mark {found_call_id} email:wrong --basis cross_checked "
    "--evidence {call_id} (MCP: mark, same fields). Keep going with the task."
)


def _header_text(text: str) -> str:
    return "".join(c if " " <= c <= "~" else " " for c in text).strip()


def mark_evidence_hint(call_id: str, found_call_id: str) -> str:
    """The evidence-time invitation: the configured text or the built-in one, ids filled in."""
    template = get_settings().mark_evidence_text.strip() or MARK_EVIDENCE_HINT
    return _header_text(template.replace("{found_call_id}", found_call_id).replace("{call_id}", call_id))


def mark_hint(call_id: str) -> tuple[int, str]:
    """(variant, text): the configured variants, or the built-in one as variant 0. The variant is
    fixed per call, so the invitation event and the text an agent saw agree. Headers carry the
    text, so it is reduced to printable ASCII."""
    texts = [t for t in get_settings().mark_hint_texts if t.strip()] or [MARK_HINT]
    variant = _bucket("mark-variant", call_id) % len(texts)
    return variant, _header_text(texts[variant].replace("{call_id}", call_id))


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
