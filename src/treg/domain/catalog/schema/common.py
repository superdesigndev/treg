"""What both spellings of an endpoint row share: the request contract and the kept keys, which keep
today's names, shapes and meanings until their module migrates."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Discriminator, Field, Tag

from .base import Date, Model, field

Json = Any


class ParamSpec(Model):
    type: str | list[str] | None = None
    required: bool | list[str] | None = None
    note: str | None = None
    example: Json = None
    enum: list[Json] | None = None
    default: Json = None
    min: int | float | None = None
    max: int | float | None = None
    minItems: int | None = None
    maxItems: int | None = None
    properties: dict[str, ParamSpec] | None = None
    item_required_all: list[str] | None = None
    item_required_any: list[str] | None = None
    authorization_methods: list[str] | None = None
    # JSON Schema words nothing reads; each row using one is on the shrink-only list
    description: str | None = None
    examples: list[Json] | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    minLength: int | None = None
    maxLength: int | None = None
    format: str | None = None
    items: ParamSpec | None = None
    anyOf: list[Json] | None = None
    additionalProperties: bool | None = None


UNREAD_SPEC_WORDS = ("description", "examples", "minimum", "maximum", "minLength", "maxLength",
                     "format", "items", "anyOf", "additionalProperties")


class ObjectBody(Model):
    type: Literal["object"]
    required: list[str] | bool | None = None
    properties: dict[str, ParamSpec]
    note: str | None = None


def _body_shape(value: object) -> str:
    if isinstance(value, ObjectBody) or (isinstance(value, dict) and "properties" in value):
        return "object"
    if isinstance(value, ParamSpec) or (isinstance(value, dict) and value
                                        and not all(isinstance(v, dict) for v in value.values())):
        return "whole"
    return "fields"


Body = Annotated[
    Annotated[dict[str, ParamSpec], Tag("fields")]
    | Annotated[ParamSpec, Tag("whole")]
    | Annotated[ObjectBody, Tag("object")],
    Discriminator(_body_shape),
]
BodyType = Literal["json", "form", "multipart"]


class Input(Model):
    queryParams: dict[str, ParamSpec] | None = None
    pathParams: dict[str, ParamSpec] | None = None
    headers: dict[str, ParamSpec] | None = None
    body: Body | None = None
    bodyType: BodyType | None = None
    queryArrayEncoding: Literal["json", "comma", "repeated"] | None = None
    note: str | None = None


class TestRequest(Model):
    x_kind = "C"
    queryParams: dict[str, Json] | None = None
    pathParams: dict[str, Json] | None = None
    headers: dict[str, Json] | None = None
    body: Json = None
    bodyType: BodyType | None = None
    files: dict[str, Json] | None = None


class RowBase(Model):
    x_kind = "C"
    id: str = field(..., min_length=1)
    capability: str | None = None
    platform: str | None = None
    domain: str | None = None
    kind: Literal["data", "action", "account", "utility"] | None = None

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = field(kind="P")
    path: str | None = field(kind="P")
    host: str | None = field(kind="P")

    name: str | None = None
    summary: str | None = field(kind="P")
    docs_url: str | None = field(kind="P")
    note: str | None = field(kind="P")
    call_note: str | None = field(kind="P")

    input: Input | None = field(kind="P")
    test_request: TestRequest | None = None
    scope: Literal["any_account", "own_account"] | None = field(kind="P")

    example_response: str | None = None
    terminal_example_response: str | None = None

    added: Date | None = None
    verified: Date | None = None
    unverified: str | None = None
    untestable: str | None = None
    skipped: str | None = None
    observed_time: int | float | None = None
    observed_from: Date | None = None
    status: Literal["retired", "broken"] | None = None
    status_note: str | None = None
    superseded_by: str | None = None

    platform_auth: Literal["anonymous"] | None = field(kind="A")
    platform_blocked: str | None = field(kind="A")
    platform_request: dict[str, Json] | None = field(kind="A")
    team_label: list[str] | None = field(kind="A")
    caller_email: list[str] | None = field(kind="A")
    scope_gap: str | None = field(kind="A")

    body_allowlist: bool | None = None
    strict_query: bool | None = None
    strict_body: bool | None = None

    cache: str | dict[str, Json] | None = field(kind="P")
    spooled_response: bool | None = field(kind="P")
    resource_ownership: dict[str, Json] | None = field(kind="P")
    managed_resource: dict[str, Json] | None = field(kind="P")
    response_retention: str | None = field(kind="P")

    authorization_method: str | None = field(kind="P")
    authorization_methods: list[str] | None = field(kind="P")
    authorization_paths: dict[str, str] | None = field(kind="P")
    required_scopes: list[str] | None = field(kind="P")
    required_resource: str | None = field(kind="P")
    token_type: str | None = field(kind="P")


class CeilingParam(Model):
    in_: Literal["query"] = Field(alias="in")
    name: str
    unit: Literal["usd"]


Statuses = Annotated[list[int], Field(min_length=1)]
