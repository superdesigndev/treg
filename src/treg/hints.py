"""Invitation negotiation and deterministic sampling without network or database access."""
import hashlib
from typing import Literal

from .config import get_settings


LEGACY_KINDS: frozenset[str] = frozenset({"review", "feedback"})
UNNAMED_KINDS: frozenset[str] = LEGACY_KINDS - {"review"}


def accepted_kinds(header: str | None, client: str = "") -> frozenset[str]:
    """Parse `X-Treg-Hints`. An empty value opts out. When omitted, a named runtime accepts
    the legacy kinds and an unnamed caller accepts feedback only."""
    if header is None:
        return LEGACY_KINDS if client else UNNAMED_KINDS
    return frozenset(k.strip().lower() for k in header.split(",") if k.strip())


def hints_header(headers) -> str | None:
    """Read and join all `X-Treg-Hints` header lines (RFC 9110); None only when absent."""
    if hasattr(headers, "getlist"):
        values = headers.getlist("X-Treg-Hints")
        return ",".join(values) if values else None
    if hasattr(headers, "get"):
        return headers.get("X-Treg-Hints")
    values = [bytes(v).decode("latin-1") for k, v in headers if bytes(k).lower() == b"x-treg-hints"]
    return ",".join(values) if values else None


def _bucket(salt: str, sample_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{sample_id}".encode()).digest()[:8], "big")


def sampled(kind: Literal["review", "feedback", "mark"], sample_id: str) -> bool:
    settings = get_settings()
    rate = {"review": settings.review_sample_rate, "feedback": settings.feedback_hint_rate,
            "mark": settings.mark_hint_rate}[kind]
    return sampled_at(kind, sample_id, rate)


def sampled_at(kind: str, sample_id: str, rate: float) -> bool:
    """The same deterministic draw at a given rate (a targeted review rate)."""
    return _bucket(kind, sample_id) < rate * 2**64


MARK_HINT = (
    "If you later learn rows of this result were wrong or right (an email bounced, another "
    "source disagreed, the user corrected it), mark them: treg mark {call_id} INDEX[/FIELD]:VERDICT "
    "(INDEX the 0-based row) --basis bounced|cross_checked|user_said, or MCP mark. Only what you checked; batching at the "
    "end is fine. Private, never changes charges. Keep going with the task."
)


def mark_hint(call_id: str) -> tuple[int, str]:
    """(variant, text): the configured variants, or the built-in one as variant 0. The variant is
    fixed per call, so the invitation event and the text an agent saw agree. Headers carry the
    text, so it is reduced to printable ASCII."""
    texts = [t for t in get_settings().mark_hint_texts if t.strip()] or [MARK_HINT]
    variant = _bucket("mark-variant", call_id) % len(texts)
    text = texts[variant].replace("{call_id}", call_id)
    return variant, "".join(c if " " <= c <= "~" else " " for c in text).strip()


HINT = (
    "Proactively share friction using treg's feedback tool. Small annoyances count: "
    "confusing results or charges, unclear instructions, or a limitation you worked around, "
    "even if your task succeeded. Describe what you observed; you do not need to prove a bug. "
    "Pass related call IDs in call_ids when available. Omit private data and report each "
    "issue once. Feedback helps the treg team improve the product; keep going and finish "
    "the user's task afterward."
)


def review_hint(call_id: str) -> str:
    """The MCP surface's words: its `review` tool takes rows."""
    return (
        f"Rate this one call after using it: review(call_id={call_id}, "
        "usefulness=useful|partly|not_useful|not_sure, reason?, rows?); one review per invitation. "
        "When partly or not_useful, rows = the 0-based rows you found wrong, e.g. "
        "[{index: 2, field: email, verdict: wrong}]. "
        "Anything confusing or wrong: feedback with this call_id in call_ids. "
        "No private data. Then continue."
    )


COMPARE_CLI_HINT = (
    "treg has two anonymous answers to an input your team sent earlier. Judge which serves that "
    "input better: treg compare shows both, then send one verdict with treg compare COMPARISON_ID "
    "1|2|tie|both_bad --reason \"one line\". No charge. Then continue."
)


def compare_cli_hint(comparison_id: int, *, input_cut: bool = False) -> str:
    """A CLI's words beside the pair it printed (`GET /comparisons/next?compact=true`)."""
    cut = (" The input was cut when stored: judge only on what it shows, and say tie when the missing "
           "part could decide it." if input_cut else "")
    return (f"Above: two anonymous answers to an input your team sent earlier. Judge which serves "
            f"that input better and send one verdict: treg compare {comparison_id} "
            "1|2|tie|both_bad --reason \"one line\". Judge what the input asked for, not length; "
            f"`shortened` answers show only their first rows.{cut} No charge. Then continue.")


def compare_inline_hint(comparison_id: int, *, input_cut: bool = False) -> str:
    """The team MCP's words when the call result carries the pair itself (`compare` in the result):
    an agent that has both answers in front of it judges them, one that must fetch them rarely does.
    `input_cut`: the stored input is incomplete, so the agent is told to judge only what it shows."""
    cut = (" The input was cut when stored: judge only on what it shows, and say tie when the missing "
           "part could decide it." if input_cut else "")
    return (f"Under `compare`: two anonymous answers to an input your team sent earlier. Judge which "
            f"serves that input better and send one verdict: compare(comparison_id={comparison_id}, "
            "choice='1'|'2'|'tie'|'both_bad', reason='one line'). Judge what the input asked for, "
            f"not length; `shortened` answers show only their first rows.{cut} No charge. Then continue.")


def header_text(kind: str, call_id: str) -> tuple[int | None, str]:
    """(variant, words) for `X-Treg-Hint-Text`: what a CLI or an HTTP caller shows, so the words
    change on the server, not in a client release. A mark has configured variants."""
    if kind == "mark":
        return mark_hint(call_id)
    if kind == "compare":  # a CLI's words; the team MCP writes its own (mcp.py)
        return None, COMPARE_CLI_HINT
    if kind == "review":
        text = (f"After using this result, rate it: treg review {call_id} "
                "<useful|partly|not_useful|not_sure> [--reason \"...\"]; when partly or not_useful "
                "add --rows INDEX[/FIELD]:VERDICT,... for the 0-based rows you found wrong "
                "(e.g. --rows 2/email:wrong). Omit private data, then keep going with the task.")
    else:
        text = ("Anything confusing or wrong about this call, even if it worked? "
                "treg feedback submit <quality|pricing|friction|other> \"what you saw\" "
                f"--call-id {call_id}; omit private data, then keep going with the task.")
    return None, text
