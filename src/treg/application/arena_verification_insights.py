"""Validate and publish aggregate audit snapshots; no evidence scans or vendor calls."""
import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from ..domain.catalog import store
from ..infra.db import session_maker
from ..models import ArenaVerificationSnapshot
from ..timeutil import utcnow_naive as now


class AuditRow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    task: Literal["people.email.find", "people.phone.find"]
    endpoint: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,160}$")
    input: Literal["name_domain", "linkedin_url", "email"]
    baseline_rate: float | None = Field(default=None, ge=0, le=100)
    baseline_n: int = Field(ge=0, le=100_000_000, strict=True)
    sample_n: int = Field(ge=1, le=10_000_000, strict=True)
    passed_n: int = Field(ge=0, le=10_000_000, strict=True)
    unresolved_n: int = Field(default=0, ge=0, le=10_000_000, strict=True)
    method: Literal["email_verifier_consensus", "phone_format"]
    verifiers: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def consistent(self):
        cat = store.load()
        endpoint = cat.by_id.get(self.endpoint)
        if not endpoint or endpoint.get("capability") != self.task:
            raise ValueError("Endpoint must match the audited task")
        if self.passed_n + self.unresolved_n > self.sample_n:
            raise ValueError("Passed count exceeds sample")
        expected = "email_verifier_consensus" if self.task == "people.email.find" else "phone_format"
        if self.method != expected:
            raise ValueError("Verification method does not match task")
        capability = "people.email.verify" if self.task == "people.email.find" else "people.phone.verify"
        providers = {ep.get("provider") for ep in cat.by_id.values() if ep.get("capability") == capability}
        if len(set(self.verifiers)) != len(self.verifiers) or not set(self.verifiers) <= providers - {"treg"}:
            raise ValueError("Unrecognized or duplicate verifier")
        if self.method == "email_verifier_consensus" and len(self.verifiers) < 2:
            raise ValueError("Consensus requires two verifiers")
        return self


class AuditSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    baseline_since: datetime
    baseline_until: datetime
    sample_since: datetime
    sample_until: datetime
    checked_at: datetime
    rows: list[AuditRow] = Field(max_length=500)

    @model_validator(mode="after")
    def ordered(self):
        dates = [self.baseline_since, self.baseline_until, self.sample_since, self.sample_until, self.checked_at]
        if any(d.tzinfo is None for d in dates):
            raise ValueError("Dates must have a timezone")
        if self.baseline_since > self.baseline_until or self.baseline_until > self.checked_at or self.sample_since > self.sample_until or self.sample_until > self.checked_at:
            raise ValueError("Invalid source date range")
        keys = [(r.task, r.endpoint, r.input) for r in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate endpoint/input aggregate")
        return self


def build_snapshot(data):
    audit = AuditSnapshot.model_validate(data)
    payload = audit.model_dump(mode="json")
    for row in payload["rows"]:
        eligible = row["method"] == "email_verifier_consensus" and row["baseline_rate"] is not None and row["baseline_n"] >= 20 and row["sample_n"] >= 20
        row["rate"] = round(row["baseline_rate"] * row["passed_n"] / row["sample_n"], 2) if eligible else None
        row["estimate"] = True
        row["small_sample"] = row["sample_n"] < 100
        # Keep the historical projection for older consumers; validity has a
        # different denominator and never depends on lookup coverage.
        row["checked_n"] = row["sample_n"] - row["unresolved_n"]
        eligible_validity = row["method"] == "email_verifier_consensus" and row["checked_n"] >= 20
        row["validity_rate"] = round(100 * row["passed_n"] / row["checked_n"], 2) if eligible_validity else None
        if row["method"] == "phone_format":
            row["format_validity_rate"] = round(100 * row["passed_n"] / row["checked_n"], 2) if row["checked_n"] >= 20 else None
    return payload


async def publish_snapshot(data, session_factory=session_maker):
    """One immutable publication per run. Reimport is a no-op; conflicting data fails."""
    payload = build_snapshot(data)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    async with session_factory() as db:
        existing = await db.get(ArenaVerificationSnapshot, payload["run_id"])
        if existing:
            if existing.source_digest != digest:
                raise ValueError("Run already published with different contents")
            return False
        db.add(ArenaVerificationSnapshot(id=payload["run_id"], source_digest=digest, published_at=now(), payload=payload))
        await db.commit()
    return True


async def public_snapshot(db):
    row = (await db.execute(select(ArenaVerificationSnapshot).order_by(
        ArenaVerificationSnapshot.published_at.desc(), ArenaVerificationSnapshot.id.desc()).limit(1))).scalar_one_or_none()
    return row.payload if row else None
