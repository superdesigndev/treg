"""Feedback intake and private reads; all writes use the application transaction."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import feedback as feedback_app
from ..domain import feedback
from ..domain.feedback import reviews
from ..domain.identity.access import Caller, require_member, require_superadmin
from ..feedback_contract import FeedbackCategory, ReviewUsefulness
from ..infra.db import get_admin_session, get_session

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
            org_id=caller.org_id, user_email=caller.email, **body.model_dump(),
        )
    except feedback_app.FeedbackRateLimited:
        raise HTTPException(429, "Too many feedback reports. Try again later.") from None
    return {"feedback_id": feedback_id, "status": "received"}


@app.get("/feedback/{feedback_id}")
async def get_feedback(
    feedback_id: int, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    row = await feedback.get(db, feedback_id, caller.org_id)
    if row is None:
        raise HTTPException(404, "Feedback not found")
    return {"feedback_id": row.id, "status": "received", "category": row.category,
            "message": row.message, "call_ids": row.call_ids, "endpoint_id": row.endpoint_id,
            "created_at": row.created_at}


@app.get("/admin/feedback", include_in_schema=False)
async def admin_feedback(
    limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, ge=1),
    category: FeedbackCategory | None = None,
    _admin: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    rows = await feedback.recent(db, limit=limit, before=before, category=category)
    return {"items": [row.model_dump() for row in rows],
            "next_before": rows[-1].id if len(rows) == limit else None}


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    call_id: CallReference
    usefulness: ReviewUsefulness
    reason: str | None = Field(default=None, min_length=1, max_length=200)


@app.post("/reviews", status_code=201)
async def submit_review(
    body: ReviewIn, request: Request, response: Response,
    caller: Caller = Depends(require_member),
) -> dict:
    try:
        review_id, inserted = await feedback_app.submit_review(
            org_id=caller.org_id, user_email=caller.email,
            client=request.headers.get("X-Treg-Client", ""), **body.model_dump(),
        )
    except feedback_app.ReviewCallNotFound:
        raise HTTPException(404, "Call record not found in this team; it may not be written yet. Retry shortly.") from None
    except feedback_app.ReviewOwnTool:
        raise HTTPException(400, "Reviews require a catalog call, not a team's own tool.") from None
    response.status_code = 201 if inserted else 200
    return {"review_id": review_id, "status": "received" if inserted else "already_reviewed"}


@app.get("/admin/reviews", include_in_schema=False)
async def admin_reviews(
    limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, ge=1),
    endpoint_id: str | None = None,
    _admin: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    rows = await reviews.recent(db, limit=limit, before=before, endpoint_id=endpoint_id)
    return {"items": [row.model_dump() for row in rows],
            "next_before": rows[-1].id if len(rows) == limit else None}
