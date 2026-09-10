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
    "Use feedback for anything confusing or wrong, omit private information, credentials and "
    "raw requests, responses or logs, and keep going with the task afterward."
)
