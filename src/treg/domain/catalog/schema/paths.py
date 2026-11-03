"""The one path syntax and the one predicate every v2 rule that reads a request or an answer uses."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal, TypeAliasType

from pydantic import AfterValidator, Discriminator, Field, Tag, model_validator

from .base import Model

LOCATIONS = ("body", "query", "header", "response")
REQUEST = frozenset({"body", "query"})
RESPONSE = frozenset({"response"})
_RESERVED = ".[]"


@dataclass(frozen=True)
class Step:
    name: str | None = None
    index: int | None = None
    every: bool = False
    filter: tuple[str, str] | None = None


@dataclass(frozen=True)
class Path:
    text: str
    location: str
    filter: tuple[str, str] | None
    steps: tuple[Step, ...]

    @property
    def many(self) -> bool:
        return self.filter is not None or any(s.every or s.filter for s in self.steps)


def _filter(text: str, at: int) -> tuple[tuple[str, str], int]:
    end = text.find("]", at)
    body = text[at + 1:end] if end != -1 else ""
    key, eq, value = body.partition("=")
    if end == -1 or not eq or not key or not value or any(c in _RESERVED for c in key):
        raise ValueError(f"{text!r}: a filter is [key=value]")
    return (key, value), end + 1


@lru_cache(maxsize=None)
def parse_path(text: str) -> Path:
    if not isinstance(text, str):
        raise ValueError("a path is a string")
    head = next((i for i, c in enumerate(text) if c in ".["), len(text))
    location = text[:head]
    if location not in LOCATIONS:
        raise ValueError(f"{text!r}: a path starts with one of {', '.join(LOCATIONS)}")
    at, root_filter = head, None
    if text[at:at + 1] == "[":
        root_filter, at = _filter(text, at)
    steps: list[Step] = []
    while at < len(text):
        if text[at] != ".":
            raise ValueError(f"{text!r}: steps are separated by '.'")
        end = next((i for i in range(at + 1, len(text)) if text[i] in ".["), len(text))
        name = text[at + 1:end]
        if not name:
            raise ValueError(f"{text!r}: an empty step")
        at = end
        if name == "*":
            steps.append(Step(every=True))
        elif name.isdigit():
            steps.append(Step(index=int(name)))
        else:
            step_filter = None
            if text[at:at + 1] == "[":
                step_filter, at = _filter(text, at)
            steps.append(Step(name=name, filter=step_filter))
    return Path(text, location, root_filter, tuple(steps))


def path_in(*locations: str):
    allowed = frozenset(locations)

    def check(text: str) -> str:
        path = parse_path(text)
        if path.location not in allowed:
            raise ValueError(f"{text!r}: a {path.location} path is not allowed here "
                             f"(allowed: {', '.join(sorted(allowed))})")
        return text

    return AfterValidator(check)


RequestPath = Annotated[str, path_in(*REQUEST)]
ResponsePath = Annotated[str, path_in(*RESPONSE)]

Scalar = str | int | float | bool | None
_VALUED = {"equals", "in", "contains"}


class Leaf(Model):
    path: Annotated[str, AfterValidator(lambda text: parse_path(text).text)]
    op: Literal["equals", "in", "contains", "present", "absent", "empty"]
    value: Scalar | list[Scalar] = Field(default=None)

    @model_validator(mode="after")
    def _value_fits_op(self) -> Leaf:
        given = "value" in self.model_fields_set
        if self.op not in _VALUED:
            if given:
                raise ValueError(f"op {self.op} takes no value")
        elif not given:
            raise ValueError(f"op {self.op} needs a value")
        elif self.op == "in" and not isinstance(self.value, list):
            raise ValueError("op in takes a list of scalars")
        elif self.op == "equals" and isinstance(self.value, list):
            raise ValueError("op equals takes a scalar")
        elif self.op == "contains" and not isinstance(self.value, str):
            raise ValueError("op contains takes a string")
        return self


class AnyOf(Model):
    any: list[Predicate] = Field(min_length=1)


def _shape(value: object) -> str:
    if isinstance(value, list):
        return "all"
    if isinstance(value, AnyOf) or (isinstance(value, dict) and "any" in value):
        return "any"
    return "leaf"


Predicate = TypeAliasType(
    "Predicate",
    Annotated[
        Annotated[Leaf, Tag("leaf")]
        | Annotated[AnyOf, Tag("any")]
        | Annotated[list["Predicate"], Tag("all"), Field(min_length=1)],
        Discriminator(_shape),
    ],
)
AnyOf.model_rebuild()


def leaves(predicate: object) -> Iterator[Leaf]:
    if isinstance(predicate, Leaf):
        yield predicate
    elif isinstance(predicate, AnyOf):
        for item in predicate.any:
            yield from leaves(item)
    elif isinstance(predicate, list):
        for item in predicate:
            yield from leaves(item)


def predicate_in(*locations: str):
    allowed = frozenset(locations)

    def check(predicate):
        for leaf in leaves(predicate):
            location = parse_path(leaf.path).location
            if location not in allowed:
                raise ValueError(f"{leaf.path!r}: a {location} path is not allowed here "
                                 f"(allowed: {', '.join(sorted(allowed))})")
        return predicate

    return AfterValidator(check)


RequestPredicate = Annotated[Predicate, predicate_in(*REQUEST)]
ResponsePredicate = Annotated[Predicate, predicate_in(*RESPONSE)]
AnswerOrRequestPredicate = Annotated[Predicate, predicate_in(*REQUEST, *RESPONSE)]
