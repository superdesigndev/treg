"""Feedback intake and private reads; all writes use the application transaction."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .. import hints
from ..application import compare as compare_app
from ..application import feedback as feedback_app
from ..application.call.invite import accepted_kinds
from ..domain import feedback
from ..domain.feedback import marks, reviews
from ..domain.governance.access import pinned_tag_predicates
from ..models import Feedback
from ..domain.identity.access import Caller, require_member, require_superadmin
from ..feedback_contract import FeedbackCategory, MarkBasis, MarkVerdict, ReviewUsefulness
from ..infra.db import get_admin_session, get_session
from . import view
from .views import account as account_views

app = APIRouter()

CallReference = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
)]


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: FeedbackCategory
    message: str = Field(min_length=1, max_length=2000)
    call_ids: list[CallReference] = Field(default_factory=list, max_length=100)
    endpoint_id: str | None = Field(default=None, min_length=1, max_length=200,
                                    pattern=r"^[a-zA-Z0-9_.-]+$")

    @field_validator("call_ids")
    @classmethod
    def distinct_call_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


@app.post("/feedback", status_code=201)
async def submit_feedback(body: FeedbackIn, caller: Caller = Depends(require_member)) -> dict:
    try:
        feedback_id = await feedback_app.submit(
            org_id=caller.org_id, user_email=caller.email,
            pinned_tags=caller.membership.pinned_tags, **body.model_dump(),
        )
    except feedback_app.FeedbackRateLimited:
        raise HTTPException(429, "Too many feedback reports. Try again later.") from None
    return {"feedback_id": feedback_id, "status": "received"}


@app.get("/feedback/{feedback_id}")
async def get_feedback(
    feedback_id: int, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    row = await feedback.get(db, feedback_id, caller.org_id,
                             *pinned_tag_predicates(Feedback.tags, caller.membership.pinned_tags))
    if row is None:
        raise HTTPException(404, "Feedback not found")
    return {"feedback_id": row.id, "status": "received", "category": row.category,
            "message": row.message, "call_ids": row.call_ids, "endpoint_id": row.endpoint_id,
            "created_at": row.created_at}


@view.renders(get_feedback)
async def _feedback_view(answer: view.Answer) -> str:
    return account_views.feedback(answer.status, answer.json())


@app.get("/admin/feedback", include_in_schema=False)
async def admin_feedback(
    limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, ge=1),
    category: FeedbackCategory | None = None,
    _admin: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    rows = await feedback.recent(db, limit=limit, before=before, category=category)
    return {"items": [row.model_dump() for row in rows],
            "next_before": rows[-1].id if len(rows) == limit else None}


class MarkItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: MarkVerdict
    # A position in the answer as the caller saw it (0-based); omitted means the whole answer.
    index: int | None = Field(default=None, ge=0, le=100_000)
    field: str | None = Field(default=None, min_length=1, max_length=120,
                              pattern=r"^[A-Za-z0-9_.\[\]-]+$")


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    call_id: CallReference
    usefulness: ReviewUsefulness
    reason: str | None = Field(default=None, min_length=1, max_length=200)
    # The rows the reviewer found wrong (or right), stored as a mark tied to this review. Its
    # basis defaults to the reviewer's impression: a review is a judgement after use, not a check.
    rows: list[MarkItem] = Field(default_factory=list, max_length=200)
    basis: MarkBasis = "impression"


@app.post("/reviews", status_code=201)
async def submit_review(
    body: ReviewIn, request: Request, response: Response,
    caller: Caller = Depends(require_member),
) -> dict:
    try:
        values = body.model_dump()
        values["rows"] = [{k: v for k, v in row.items() if v is not None} for row in values["rows"]]
        review_id, inserted, mark_id = await feedback_app.submit_review(
            org_id=caller.org_id, user_email=caller.email,
            client=request.headers.get("X-Treg-Client", ""),
            shows_compare="compare" in accepted_kinds(request.headers.get("X-Treg-Hints")),
            pinned_tags=caller.membership.pinned_tags, **values,
        )
    except feedback_app.ReviewCallNotFound:
        raise HTTPException(404, "Call record not found in this team; it may not be written yet. Retry shortly.") from None
    except feedback_app.ReviewOwnTool:
        raise HTTPException(400, "Reviews require a catalog call, not a team's own tool or its own hub tool.") from None
    except feedback_app.FeedbackRateLimited:
        raise HTTPException(429, "Too many marks; nothing was saved. Retry later, or review without rows.") from None
    response.status_code = 201 if inserted else 200
    out = {"review_id": review_id, "status": "received" if inserted else "already_reviewed"}
    if mark_id is not None:
        out["mark_id"] = mark_id
    return out


@app.get("/admin/reviews", include_in_schema=False)
async def admin_reviews(
    limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, ge=1),
    endpoint_id: str | None = None,
    _admin: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    rows = await reviews.recent(db, limit=limit, before=before, endpoint_id=endpoint_id)
    return {"items": [row.model_dump() for row in rows],
            "next_before": rows[-1].id if len(rows) == limit else None}


class MarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    call_id: CallReference
    basis: MarkBasis
    items: list[MarkItem] = Field(default_factory=list, max_length=200)
    checked: int | None = Field(default=None, ge=0, le=100_000)
    evidence_call_id: CallReference | None = None
    note: str | None = Field(default=None, min_length=1, max_length=500)
    replaces: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def says_something(self) -> "MarkIn":
        # An empty mark is only a withdrawal of an earlier one.
        if not self.items and self.replaces is None:
            raise ValueError("items is required unless the mark replaces an earlier one")
        return self


@app.post("/marks", status_code=201)
async def submit_mark(
    body: MarkIn, request: Request, response: Response,
    caller: Caller = Depends(require_member),
) -> dict:
    values = body.model_dump()
    values["items"] = [{k: v for k, v in item.items() if v is not None} for item in values["items"]]
    try:
        mark_id, inserted = await feedback_app.submit_mark(
            org_id=caller.org_id, user_email=caller.email,
            client=request.headers.get("X-Treg-Client", ""),
            pinned_tags=caller.membership.pinned_tags, **values,
        )
    except feedback_app.FeedbackRateLimited:
        raise HTTPException(429, "Too many marks. Try again later.") from None
    except feedback_app.ReviewCallNotFound:
        raise HTTPException(404, "Call record not found in this team; it may not be written yet. Retry shortly.") from None
    except feedback_app.ReviewOwnTool:
        raise HTTPException(400, "Marks require a catalog call, not a team's own tool or its own hub tool.") from None
    except feedback_app.MarkReplacesUnknown:
        raise HTTPException(404, "replaces must name an earlier mark of this team on the same call.") from None
    response.status_code = 201 if inserted else 200
    return {"mark_id": mark_id, "status": "received" if inserted else "already_received"}


@app.get("/admin/marks", include_in_schema=False)
async def admin_marks(
    limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, ge=1),
    endpoint_id: str | None = None,
    _admin: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    rows = await marks.recent(db, limit=limit, before=before, endpoint_id=endpoint_id)
    return {"items": [row.model_dump() for row in rows],
            "next_before": rows[-1].id if len(rows) == limit else None}


@app.get("/comparisons/next")
async def next_comparison(caller: Caller = Depends(require_member), compact: bool = False):
    """The team's newest comparison waiting for a verdict, blinded: answer 1 and 2, no vendor.
    `compact`: the pair as a CLI prints it beside a call (`compare_app.compact_pair`), with the
    words that ask for the verdict (`how_to_vote`), so they change without a CLI release."""
    pair = await compare_app.next_for(caller.org_id, pinned=caller.membership.pinned_tags is not None)
    if pair is None:
        return Response(status_code=204)
    if not compact:
        return pair
    shown = compare_app.compact_pair(pair)
    shown["how_to_vote"] = hints.compare_cli_hint(
        shown["comparison_id"], input_cut="input" in shown.get("truncated_parts", []))
    return shown


class ComparisonIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    comparison_id: int = Field(ge=1)
    choice: Literal["1", "2", "tie", "both_bad"]
    reason: str | None = Field(default=None, min_length=1, max_length=300)


@app.post("/comparisons", status_code=201)
async def submit_comparison(
    body: ComparisonIn, request: Request, response: Response,
    caller: Caller = Depends(require_member),
) -> dict:
    try:
        choice, inserted = await compare_app.submit(
            caller.org_id, pinned=caller.membership.pinned_tags is not None,
            comparison_id=body.comparison_id, choice=body.choice, reason=body.reason,
            client=request.headers.get("X-Treg-Client", ""))
    except compare_app.ComparisonNotFound:
        raise HTTPException(404, "Comparison not found in this team.") from None
    response.status_code = 201 if inserted else 200
    return {"comparison_id": body.comparison_id, "choice": choice,
            "status": "received" if inserted else "already_received"}
