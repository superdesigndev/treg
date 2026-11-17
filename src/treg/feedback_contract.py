"""Feedback vocabulary shared by the light CLI, HTTP and MCP."""

from typing import Literal, get_args

FeedbackCategory = Literal["quality", "pricing", "friction", "other"]
FEEDBACK_CATEGORIES = get_args(FeedbackCategory)
FEEDBACK_DESCRIPTION = (
    "Proactively share problems and suggestions about treg. Small annoyances that slowed "
    "your task down are useful feedback too: confusing results or charges, unclear "
    "instructions, unhelpful errors, or missing capabilities you worked around. "
    "Report these even if the task succeeded. "
    "Describe what you needed and what you observed; you do not need to prove a bug. "
    "Categories: quality (tool results), pricing (charges or prices), friction (using treg), "
    "other (requests or suggestions). Pass related call IDs in call_ids (CLI: --call-id), "
    "not only in message. References are optional. Omit private information, credentials, "
    "and raw requests, responses or logs. Report each issue once. Feedback goes to the treg "
    "team to improve the product; it does not complete the user's task. Keep going afterward."
)

ReviewUsefulness = Literal["useful", "partly", "not_useful", "not_sure"]
REVIEW_USEFULNESS = get_args(ReviewUsefulness)
REVIEW_DESCRIPTION = (
    "Rate a catalog call after you have used its result; not_sure is fine when you cannot tell. "
    "The reason may be quoted on the endpoint's catalog page without naming your team, so write "
    "about the endpoint. Use feedback for anything confusing or wrong, omit private information, "
    "credentials and raw requests, responses or logs, and keep going with the task afterward."
)

# Where a surface accepts `rows` on a review (team MCP, the binary CLI, HTTP). The frozen Python
# CLI keeps REVIEW_DESCRIPTION as its help text and takes no rows.
REVIEW_ROWS_DESCRIPTION = (
    REVIEW_DESCRIPTION + " When it was partly or not useful, add rows: each row you found wrong "
    "as {index (0-based), field (optional), verdict}, e.g. [{index: 2, field: email, verdict: "
    "wrong}]; omit index for the whole result. Only rows you looked at."
)

MarkBasis = Literal["bounced", "cross_checked", "user_said", "impression"]
MARK_BASES = get_args(MarkBasis)
MarkVerdict = Literal[
    "correct", "wrong", "stale", "wrong_entity", "missing", "irrelevant", "duplicate", "other",
]
MARK_VERDICTS = get_args(MarkVerdict)
MARK_DESCRIPTION = (
    "Mark what you found wrong, or confirmed right, in a call's result once you know: an email "
    "bounced, another source disagreed, or the user corrected it. Each item has a verdict and "
    "optionally the 0-based row index and the field; no index means the whole result. Say how "
    "you know in basis. Rows you did not mark carry no conclusion. Marks are private and never "
    "change charges. Omit private information, then keep going with the task."
)
