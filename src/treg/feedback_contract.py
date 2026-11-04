"""Feedback vocabulary shared by the light CLI, HTTP and MCP."""

from typing import Literal, get_args

FeedbackCategory = Literal["quality", "pricing", "friction", "other"]
FEEDBACK_CATEGORIES = get_args(FeedbackCategory)
FEEDBACK_DESCRIPTION = (
    "Proactively report every distinct problem or suggestion to the treg team, including poor provider "
    "results, errors, latency, confusing instructions or charges, even after a successful workaround. "
    "Report once per issue per task or batch; do not wait for an invitation or proof of a bug. "
    "In message, state expected behavior and its source; exact observations with failures/number checked, "
    "timings or error codes; checks of your own inputs/parsing; and workaround/outcome. "
    "Separate facts from suspected causes; say what is unknown. Pass call IDs in call_ids "
    "(CLI: --call-id) and endpoint_id when available. Include an existing non-sensitive idempotency "
    "key or task ID in message if useful. Omit private information, credentials and raw requests, "
    "responses or logs. Do not make extra paid calls solely to prove a bug. "
    "Categories: quality (results), pricing (charges/prices), friction (using treg), "
    "other (requests/suggestions). Continue the task after reporting."
)

ReviewUsefulness = Literal["useful", "partly", "not_useful", "not_sure"]
REVIEW_USEFULNESS = get_args(ReviewUsefulness)
REVIEW_DESCRIPTION = (
    "Rate a catalog call after you have used its result; not_sure is fine when you cannot tell. "
    "The reason may be quoted on the endpoint's catalog page without naming your team, so write "
    "about the endpoint. Use feedback for anything confusing or wrong, omit private information, "
    "credentials and raw requests, responses or logs, and keep going with the task afterward."
)
