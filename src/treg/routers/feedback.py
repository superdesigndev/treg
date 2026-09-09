"""Feedback intake and private reads; all writes use the application transaction."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import feedback as feedback_app
from ..domain import feedback, toolrequest
from ..domain.identity.access import Caller, require_member, require_superadmin
from ..feedback_contract import FeedbackCategory
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


TOOLREQ_SOURCES = {"web", "cli", "mcp", "claude-connector", "api"}
TOOLREQ_STATUSES = {"open", "done", "dismissed"}


@app.get("/admin/tool-requests", include_in_schema=False)
async def admin_tool_requests(
    limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, ge=1),
    status: str | None = Query(None), source: str | None = Query(None),
    _admin: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    if status is not None and status not in TOOLREQ_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(TOOLREQ_STATUSES)}")
    if source is not None and source not in TOOLREQ_SOURCES:
        raise HTTPException(400, f"source must be one of {sorted(TOOLREQ_SOURCES)}")
    rows = await toolrequest.recent(db, limit=limit, before=before, status=status, source=source)
    return {"items": [{
        "id": r.id, "created_at": r.created_at, "capability": r.capability, "query": r.query,
        "note": r.note, "contact": r.contact, "source": r.source, "status": r.status,
        "org_id": r.org_id, "user_email": r.user_email,
    } for r in rows], "next_before": rows[-1].id if len(rows) == limit else None}
