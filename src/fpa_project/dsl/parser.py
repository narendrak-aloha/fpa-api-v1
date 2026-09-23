from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from .ast import Aggregate, Comparison, Literal, Measure, Period, PlanRef, Query, TimeFunction
from .errors import ParseError
from .lexer import Token, tokenize


TIME_FUNCTIONS = {"PRIOR", "LEAD", "YOY", "CAGR", "YTD", "QTD", "MTD", "ROLLING"}
# Parsed, never accepted: the compiler turns these into a type error that
# names the measure's aggregation type. See Compiler.validate_measure.
AGGREGATES = {"SUM", "AVG", "MIN", "MAX"}


class Parser:
    def __init__(self, source: str):
        self.tokens = tokenize(source)
        self.index = 0

    @property
    def token(self) -> Token:
        return self.tokens[self.index]

    def accept(self, value: str) -> Token | None:
        # Consuming is centralized so every grammar branch advances the same
        # token cursor and reports positions consistently.
        if self.token.value.upper() == value.upper():
            token = self.token
            self.index += 1
            return token
        return None

    def expect(self, value: str) -> Token:
        # Required keywords/operators fail immediately at the current token.
        token = self.accept(value)
        if not token:
            raise ParseError(f"expected {value!r} at position {self.token.position}")
        return token

    def expect_kind(self, kind: str) -> Token:
        # Kind checks distinguish identifiers, literals, and numeric syntax.
        if self.token.kind != kind:
            raise ParseError(f"expected {kind} at position {self.token.position}")
        token = self.token
        self.index += 1
        return token

    def parse(self) -> Query:
        # Parse clauses in the documented order so the AST has unambiguous
        # semantics for validation and compilation.
        self.expect("SELECT")
        measures = [self.parse_measure()]
        while self.accept(","):
            measures.append(self.parse_measure())

        dimensions: list[str] = []
        predicates: list[Comparison] = []
        connectors: list[str] = []
        period = as_of = plan = None
        bridge = False
        limit = None

        if self.accept("BY"):
            dimensions.append(self.expect_kind("IDENT").value)
            while self.accept(","):
                dimensions.append(self.expect_kind("IDENT").value)
        if self.accept("WHERE"):
            predicates.append(self.parse_comparison())
            while self.token.value.upper() in {"AND", "OR"}:
                connectors.append(self.token.value.upper())
                self.index += 1
                predicates.append(self.parse_comparison())
        if self.accept("FOR"):
            self.expect("PERIOD")
            period = self.parse_period()
        if self.accept("AS"):
            self.expect("OF")
            as_of = self.parse_string()
            self.validate_timestamp(as_of)
        if self.accept("COMPARE"):
            # Plan comparison is explicit: no hidden default version is used.
            self.expect("PLAN")
            plan = self.parse_plan()
            self.expect("TO")
            self.expect("ACTUAL")
        if self.accept("BRIDGE"):
            bridge = True
        if self.accept("LIMIT"):
            raw = self.expect_kind("NUMBER").value
            if "." in raw:
                raise ParseError("LIMIT must be an integer")
            limit = int(raw)
        if self.token.kind != "EOF":
            raise ParseError(f"unexpected token {self.token.value!r} at position {self.token.position}")
        if bridge and plan is None:
            raise ParseError("BRIDGE requires COMPARE PLAN ... TO ACTUAL")
        return Query(tuple(measures), tuple(dimensions), tuple(predicates), tuple(connectors), period, as_of, plan, bridge, limit)

    def parse_measure(self) -> Measure:
        # A measure may be a base metric or a nested time-function expression.
        if self.token.kind != "IDENT":
            raise ParseError(f"expected measure at position {self.token.position}")
        name = self.token.value
        self.index += 1
        value: str | TimeFunction | Aggregate
        if name.upper() in TIME_FUNCTIONS:
            value = self.parse_time_function(name)
        elif name.upper() in AGGREGATES:
            self.expect("(")
            metric = self.expect_kind("IDENT").value
            self.expect(")")
            value = Aggregate(name.upper(), metric)
        else:
            value = name
        alias = None
        if self.token.value.upper() == "AS" and self.tokens[self.index + 1].value.upper() != "OF":
            self.index += 1
            alias = self.expect_kind("IDENT").value
        return Measure(value, alias)

    def parse_time_function(self, name: str) -> TimeFunction:
        # Preserve time functions as typed AST nodes; SQL window semantics are
        # selected later by the compiler.
        self.expect("(")
        if self.token.kind != "IDENT":
            raise ParseError("time function requires a metric or nested time function")
        child = self.token.value
        self.index += 1
        metric: str | TimeFunction = self.parse_time_function(child) if child.upper() in TIME_FUNCTIONS else child
        offset = None
        if self.accept(","):
            offset = Decimal(self.expect_kind("NUMBER").value)
        self.expect(")")
        return TimeFunction(name.upper(), metric, offset)

    def parse_comparison(self) -> Comparison:
        # Parse values as literals, keeping user data out of SQL syntax.
        field = self.expect_kind("IDENT").value
        operator = self.token.value.upper()
        if operator not in {"=", "!=", ">", ">=", "<", "<=", "IN", "NOT"}:
            raise ParseError(f"expected comparison operator at position {self.token.position}")
        self.index += 1
        if operator == "NOT":
            self.expect("IN")
            operator = "NOT IN"
        if operator in {"IN", "NOT IN"}:
            self.expect("(")
            values = [self.parse_literal()]
            while self.accept(","):
                values.append(self.parse_literal())
            self.expect(")")
        else:
            values = [self.parse_literal()]
        return Comparison(field, operator, tuple(values))

    def parse_literal(self) -> Literal:
        # Decimal conversion happens here so numeric semantics are fixed before
        # the compiler chooses a ClickHouse parameter type.
        if self.token.kind == "STRING":
            return Literal(self.parse_string(), True)
        raw = self.expect_kind("NUMBER").value
        try:
            return Literal(Decimal(raw), False)
        except InvalidOperation as exc:
            raise ParseError(f"invalid number: {raw}") from exc

    def parse_string(self) -> str:
        raw = self.expect_kind("STRING").value[1:-1]
        return raw.replace("''", "'")

    def parse_period(self) -> Period:
        # Period ranges are converted to half-open date intervals by the
        # compiler, avoiding month-end timestamp edge cases.
        start = self.parse_period_atom()
        end = start
        if self.token.kind == "DOTDOT":
            self.index += 1
            end = self.parse_period_atom()
        return Period(start, end)

    def parse_period_atom(self) -> str:
        year = self.expect_kind("NUMBER").value
        if len(year) != 4 or "." in year:
            raise ParseError("period year must be four digits")
        if not self.accept("-"):
            return year
        if self.token.kind not in {"IDENT", "NUMBER", "PERIOD_MONTH"}:
            raise ParseError(f"expected period suffix at position {self.token.position}")
        suffix = self.token.value
        self.index += 1
        if suffix in {"Q1", "Q2", "Q3", "Q4", "H1", "H2"}:
            return year + "-" + suffix
        if len(suffix) == 2 and suffix.isdigit() and 1 <= int(suffix) <= 12:
            return year + "-" + suffix
        raise ParseError(f"invalid period suffix: {suffix}")

    def parse_plan(self) -> PlanRef:
        # Plan version is mandatory; scenario defaults only after the explicit
        # version has been supplied.
        self.expect("pv")
        self.expect("=")
        version = self.parse_string()
        scenario = "base"
        if self.accept(","):
            self.expect("scenario")
            self.expect("=")
            scenario = self.parse_string()
        return PlanRef(version, scenario)

    @staticmethod
    def validate_timestamp(value: str) -> None:
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        except ValueError as exc:
            raise ParseError("AS OF requires YYYY-MM-DDTHH:MM:SS") from exc


def parse_query(source: str) -> Query:
    return Parser(source).parse()
