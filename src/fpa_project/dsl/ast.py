from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Union


@dataclass(frozen=True)
class Literal:
    value: Union[str, Decimal]
    is_string: bool


@dataclass(frozen=True)
class TimeFunction:
    name: str
    metric: str | TimeFunction
    offset: Decimal | None = None


@dataclass(frozen=True)
class Aggregate:
    """An explicit SUM/AVG/MIN/MAX around a measure in a query.

    Kept as its own node rather than rejected at parse time so the compiler
    can say *why* it is wrong: ``SUM(utilisation)`` is a type error about
    ratio measures, and the message has to explain that, not report an
    unexpected token.
    """

    name: str
    metric: str


@dataclass(frozen=True)
class Measure:
    name: str | TimeFunction | Aggregate
    alias: str | None = None


@dataclass(frozen=True)
class Comparison:
    field: str
    operator: str
    values: tuple[Literal, ...]


@dataclass(frozen=True)
class Period:
    start: str
    end: str


@dataclass(frozen=True)
class PlanRef:
    version: str
    scenario: str = "base"


@dataclass(frozen=True)
class Query:
    measures: tuple[Measure, ...]
    dimensions: tuple[str, ...] = ()
    predicates: tuple[Comparison, ...] = ()
    predicate_connectors: tuple[str, ...] = ()
    period: Period | None = None
    as_of: str | None = None
    plan: PlanRef | None = None
    bridge: bool = False
    limit: int | None = None
