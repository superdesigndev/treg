"""The JSON Schema export for editors: an endpoint file (rows in either spelling) and a manifest,
every field tagged with its kind (`x-kind`) and credential placement with `x-gated`."""

from __future__ import annotations

from pydantic import Field

from .common import Json
from .legacy import EndpointFile, LegacyRow
from .manifest import Manifest
from .v2 import V2Row


class _EndpointFileSchema(EndpointFile):
    endpoints: list[LegacyRow | V2Row] = Field(min_length=1)  # type: ignore[assignment]
    defaults: dict[str, Json] | None = None  # type: ignore[assignment]


def json_schemas() -> dict[str, dict]:
    return {
        "endpoint-file.schema.json": _EndpointFileSchema.model_json_schema(by_alias=True),
        "manifest.schema.json": Manifest.model_json_schema(by_alias=True),
    }
