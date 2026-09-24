"""FinOpsExpr query AST -> parameterised ClickHouse SQL.

The compiler is the only path from a model's output to the database, so
everything a model could get wrong is checked here before any SQL exists:
names against the registry, aggregation against the measure's type, scope
against the caller, cost against a budget. Every literal is a bound parameter.

Reading the ledger
------------------
``fact_gl_actual`` holds two vintages of the same keys, and reading it is the
one place this compiler has an opinion that is not in the grammar. A row is
visible *as of* a close when its ``_version`` is at or below that close's
vintage, the newest such row wins, and a deletion marker hides the key. That
is what ``actual_source`` builds, and it builds it the same way whether or not
the query said ``AS OF``: a current read is an as-of read at the latest close.
Two reports on the same quarter can then only disagree because the books did.

Time functions
--------------
``YOY``, ``PRIOR`` and the rest need a monthly series, so a query that uses one
is compiled in three layers: aggregate per month, apply the window, then keep
the months that were asked for. The window frames are ``RANGE`` frames over a
month number rather than ``ROWS`` frames, so a group with a missing month does
not have its lag silently shifted onto the wrong period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .ast import Aggregate, Comparison, Literal, Measure, Period, Query, TimeFunction
from .errors import DSLValidationError
from .parser import parse_query
from .schema import Schema

ACTUAL_KEY = ("company", "period_month", "account", "dim_signature_hash")
# A restatement rewrites a line in place, keeping its voucher_no, so the
# voucher is part of a row's identity: two vouchers on one key are two
# postings, not a row and its correction.
ACTUAL_DEDUP_KEY = (*ACTUAL_KEY, "voucher_no")

# How far outside the requested period a time function has to read.
# (months before the start, months after the end)
_LOOKBACK = {"YOY": 12, "MTD": 0, "QTD": 0, "YTD": 0}


@dataclass(frozen=True)
class SecurityContext:
    """Authorisation and resource limits supplied by the authenticated caller."""

    allowed_companies: frozenset[str] | None = None
    max_estimated_rows: int = 1_000_000

    def __post_init__(self) -> None:
        if self.max_estimated_rows < 0:
            raise ValueError("max_estimated_rows must be non-negative")
        if self.allowed_companies is not None and not all(
            isinstance(company, str) and company for company in self.allowed_companies
        ):
            raise ValueError("allowed_companies must contain non-empty strings")


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: dict[str, Any]
    estimated_rows: int = 0
    # The AS OF timestamp, or "current". The vintage *number* it resolves to
    # comes from the database; see vintage_lookup.
    vintage: str = "current"
    # True when the result has one row per month (a time function was used).
    monthly: bool = False
    # True when the rows are matched plan/actual lines for the bridge module.
    bridge: bool = False


def vintage_lookup(as_of: str | None) -> tuple[str, dict[str, Any]]:
    """The query that names the vintage a read ran on.

    Kept out of the compiled SQL so the compiler stays pure. The caller runs
    this and refuses the read if it returns no row: an AS OF before the first
    close is an error, not an empty answer.
    """
    if as_of is None:
        return (
            "SELECT vintage, closed_at, note FROM fpa_cube.dim_ledger_vintage ORDER BY closed_at DESC LIMIT 1",
            {},
        )
    return (
        "SELECT vintage, closed_at, note FROM fpa_cube.dim_ledger_vintage "
        "WHERE closed_at <= {as_of:DateTime} ORDER BY closed_at DESC LIMIT 1",
        {"as_of": clickhouse_datetime(as_of)},
    )


class Compiler:
    def __init__(self, schema: Schema | None = None, security_context: SecurityContext | None = None):
        self.schema = schema or Schema()
        self.security_context = security_context or SecurityContext()
        self.params: dict[str, Any] = {}
        self.param_index = 0

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    def compile(self, query: Query) -> CompiledQuery:
        # Validate before producing SQL; callers should never receive a
        # partially trusted query representation.
        self.validate_query(query)
        if query.bridge:
            return self.compile_bridge(query)
        estimated = self.estimate_rows(query)
        self.enforce_budget(estimated)
        if any(isinstance(m.name, TimeFunction) for m in query.measures):
            sql = self.compile_monthly(query)
            return CompiledQuery(sql, self.params, estimated, query.as_of or "current", monthly=True)
        return CompiledQuery(self.compile_plain(query), self.params, estimated, query.as_of or "current")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_query(self, query: Query) -> None:
        # Keep semantic validation separate from SQL generation so invalid
        # names and combinations fail before a database call is possible.
        if query.plan and query.plan.scenario not in self.schema.scenarios:
            raise DSLValidationError(f"unknown scenario: {query.plan.scenario}")
        for dimension in query.dimensions:
            self.schema.require_dimension(dimension)
        for comparison in query.predicates:
            self.validate_comparison(comparison)
        for measure in query.measures:
            self.validate_measure(measure, query)
        if query.bridge and query.plan is None:
            raise DSLValidationError("BRIDGE requires a plan comparison")
        if query.as_of and query.plan and not query.bridge:
            raise DSLValidationError("AS OF is only valid for actual-ledger queries")
        if query.limit is not None and query.limit < 0:
            raise DSLValidationError("LIMIT must be non-negative")
        if query.period and self.period_start(query.period.start) >= self.period_end(query.period.end):
            raise DSLValidationError("period range must end after it starts")
        for measure in query.measures:
            if measure.alias is not None and not self.is_safe_identifier(measure.alias):
                raise DSLValidationError(f"invalid measure alias: {measure.alias}")
        if query.bridge and any(isinstance(m.name, (TimeFunction, Aggregate)) for m in query.measures):
            raise DSLValidationError("BRIDGE decomposes plain measures; time functions and aggregates are not bridged")
        if query.bridge and query.limit is not None:
            # A bridge over the first N lines ties perfectly and explains the
            # wrong gap. Only the whole matched set is a bridge.
            raise DSLValidationError(
                "LIMIT does not apply to BRIDGE: the bridge has to run over every matched line to explain the gap. "
                "Narrow it with WHERE or fewer BY levels instead."
            )
        if "OR" in query.predicate_connectors and len(
            {c.field in self.schema.metric_names for c in query.predicates}
        ) > 1:
            # Dimension filters are decided per row (WHERE) and measure filters
            # per group (HAVING); an OR across the two cannot be split between
            # them without changing its meaning.
            raise DSLValidationError(
                "OR cannot join a dimension filter and a measure filter: dimensions are filtered per row and "
                "measures per group. Use AND between them, or ask two questions."
            )

    def validate_measure(self, measure: Measure, query: Query) -> None:
        name = measure.name
        if isinstance(name, Aggregate):
            self.validate_aggregate(name)
            return
        base = self.base_metric(name)
        self.schema.require_metric(base)
        kind = self.schema.metrics[base].get("kind")
        if isinstance(name, TimeFunction):
            if isinstance(name.metric, TimeFunction):
                raise DSLValidationError(
                    f"nested time functions are not supported in a query: {name.name}({name.metric.name}(...)). "
                    "Ask for one time function per measure."
                )
            if name.name in {"PRIOR", "LEAD", "ROLLING", "CAGR"} and name.offset is not None and name.offset <= 0:
                raise DSLValidationError(f"{name.name} offset must be positive")
            if name.name in {"ROLLING", "YTD", "QTD"} and kind == "ratio":
                raise DSLValidationError(
                    f"{name.name}({base}) sums across periods, and {base} is a ratio measure: it is recomputed "
                    "from its own numerator and denominator at whatever grain it is asked for, never summed. "
                    "Ask for the numerator and denominator instead."
                )
            if name.name in {"ROLLING", "YTD", "QTD"} and kind == "semi_additive":
                raise DSLValidationError(
                    f"{name.name}({base}) sums across periods, and {base} is semi-additive: it sums across "
                    "dimensions but never across time. Ask for it at a single closing period."
                )
            if query.period is None:
                raise DSLValidationError(f"{name.name} needs FOR PERIOD to know which months to produce")
        elif kind == "semi_additive" and query.period and query.period.start != query.period.end:
            raise DSLValidationError(
                f"semi-additive measure requires a closing period: {base} sums across dimensions but never "
                "across time, so ask for one period and it is read at that close"
            )

    def validate_aggregate(self, aggregate: Aggregate) -> None:
        """An explicit SUM/AVG/MIN/MAX in a query.

        Aggregation is decided by the measure's registered type, not by the
        query, so the only accepted form is the redundant one: SUM of an
        additive measure. Everything else is a type error, and the message
        says what the type means rather than that a token was unexpected.
        """
        metric = aggregate.metric
        self.schema.require_metric(metric)
        kind = self.schema.metrics[metric].get("kind")
        if kind == "ratio":
            raise DSLValidationError(
                f"{aggregate.name}({metric}) is a type error: {metric} is a ratio measure. It is never summed and "
                "never averaged across groups; it is recomputed from its own numerator and denominator at whatever "
                f"grain it is asked for. Ask for {metric} without {aggregate.name}."
            )
        if kind == "semi_additive":
            raise DSLValidationError(
                f"{aggregate.name}({metric}) is a type error: {metric} is semi-additive. It sums across dimensions "
                "but never across time; ask for it at a single closing period, without an aggregate."
            )
        if aggregate.name != "SUM":
            raise DSLValidationError(
                f"{aggregate.name}({metric}) is not a query operation: {metric} is additive and its aggregation is "
                "fixed by the registry as a sum. Ask for it without the aggregate."
            )

    def validate_comparison(self, comparison: Comparison) -> None:
        # Predicates may target dimensions or measures, but membership must
        # contain at least one literal to produce valid SQL.
        if comparison.field not in self.schema.dimensions and comparison.field not in self.schema.metric_names:
            raise DSLValidationError(f"unknown predicate field: {comparison.field}")
        if comparison.field in self.schema.metric_names:
            self.schema.require_metric(comparison.field)
        if comparison.operator in {"IN", "NOT IN"} and not comparison.values:
            raise DSLValidationError("membership predicate cannot be empty")

    @staticmethod
    def base_metric(value: str | TimeFunction | Aggregate) -> str:
        while isinstance(value, TimeFunction):
            value = value.metric
        return value.metric if isinstance(value, Aggregate) else value

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------
    def vintage_sql(self, as_of: str | None) -> str:
        """The vintage a read sees, as a scalar subquery.

        With no AS OF that is the latest close. With one, it is the latest
        close at or before the timestamp; an empty result makes the read
        return nothing, and vintage_lookup lets the caller turn that into an
        error rather than an empty answer.
        """
        if as_of is None:
            return "(SELECT max(vintage) FROM fpa_cube.dim_ledger_vintage)"
        return (
            "(SELECT max(vintage) FROM fpa_cube.dim_ledger_vintage WHERE closed_at <= "
            + self.bind(clickhouse_datetime(as_of), "DateTime") + ")"
        )

    def actual_source(self, alias: str, inner_predicates: list[str], as_of: str | None) -> str:
        """``fact_gl_actual`` as it stood at a close.

        ``LIMIT 1 BY`` the identity after ordering by version picks the newest
        row visible at that vintage; the caller filters ``_is_deleted`` on the
        way out. Deduplicating on the dimension key alone would collapse two
        separate postings into one, so the voucher is part of the identity.
        Roughly 1,675 rows still tie on identity and version, and the amount
        and quantity tail makes that pick reproducible rather than dependent
        on how the parts happen to be merged.
        """
        where = [*inner_predicates, f"_version <= {self.vintage_sql(as_of)}"]
        return (
            "(SELECT * FROM fpa_cube.fact_gl_actual WHERE " + " AND ".join(where)
            + " ORDER BY _version DESC, amount_functional DESC, quantity DESC"
              " LIMIT 1 BY " + ", ".join(ACTUAL_DEDUP_KEY)
            + f") AS {alias}"
        )

    def actual_rollup(self, alias: str, inner_predicates: list[str], as_of: str | None,
                      dims: list[str]) -> str:
        """Version-resolved actuals summed to the signature grain.

        A signature can carry more than one posting. The bridge sets one plan
        line against the actuals that landed on its key, so the postings are
        summed and the price is the amount-weighted average, which is what a
        price and volume split of that line means. Deduplicating to a single
        voucher here would compare the plan against part of the actual.
        """
        grouped = list(dict.fromkeys([*ACTUAL_KEY, "functional_currency", *dims]))
        cols = ", ".join(grouped)
        return (
            "(SELECT " + cols + ", sum(v.quantity) AS quantity, "
            "sum(v.amount_functional) AS amount_functional, "
            "sum(v.amount_functional) / nullIf(sum(v.quantity), 0) AS unit_price FROM "
            + self.actual_source("v", inner_predicates, as_of)
            + " WHERE v._is_deleted = 0 GROUP BY " + cols
            + f") AS {alias}"
        )

    def plan_source(self, alias: str) -> str:
        return f"fpa_cube.fact_plan_line AS {alias} FINAL"

    # ------------------------------------------------------------------
    # Plain and monthly queries
    # ------------------------------------------------------------------
    def compile_plain(self, query: Query) -> str:
        alias = "p" if query.plan else "a"
        select_parts = [
            f"{self.measure_sql(m.name, alias)} AS {m.alias or self.default_alias(m.name)}"
            for m in query.measures
        ]
        group_parts = list(query.dimensions)
        outer_where, having = self.metric_predicates(query, alias)

        if query.plan:
            source = self.plan_source(alias)
            outer_where = self.dimension_predicates(query, alias, query.period) + outer_where
        else:
            source = self.actual_source(alias, self.dimension_predicates(query, None, query.period), query.as_of)
            outer_where = [f"{alias}._is_deleted = 0", *outer_where]

        sql = f"SELECT {', '.join(group_parts + select_parts)} FROM {source}"
        if outer_where:
            sql += " WHERE " + " AND ".join(outer_where)
        if group_parts:
            sql += " GROUP BY " + ", ".join(group_parts)
        if having:
            sql += " HAVING " + self.combine_predicates(having, query)
        if query.limit is not None:
            sql += f" LIMIT {query.limit}"
        return sql

    def compile_monthly(self, query: Query) -> str:
        """Three layers: per-month aggregates, windows, then the asked-for months."""
        assert query.period is not None
        alias = "p" if query.plan else "a"
        dims = list(query.dimensions)

        lookback, lookahead, to_year_start = 0, 0, False
        for measure in query.measures:
            name = measure.name
            if not isinstance(name, TimeFunction):
                continue
            offset = int(name.offset or (1 if name.name in {"PRIOR", "LEAD", "CAGR"} else 3))
            if name.name == "PRIOR":
                lookback = max(lookback, offset)
            elif name.name == "LEAD":
                lookahead = max(lookahead, offset)
            elif name.name == "ROLLING":
                lookback = max(lookback, offset - 1)
            elif name.name == "CAGR":
                lookback = max(lookback, 12 * offset)
            elif name.name == "YOY":
                lookback = max(lookback, 12)
            elif name.name == "YTD":
                to_year_start = True
            elif name.name == "QTD":
                lookback = max(lookback, 2)

        start = self.period_start(query.period.start)
        end = self.period_end(query.period.end)
        wide_start = f"{start[:4]}-01-01" if to_year_start else self.add_months(start, -lookback)
        if to_year_start and lookback:
            wide_start = min(wide_start, self.add_months(start, -lookback))
        wide_end = self.add_months(end, lookahead)
        wide = Period(wide_start, wide_end)

        # Layer 1: one row per (dims, month) over the widened range.
        bases = sorted({self.base_metric(m.name) for m in query.measures})
        base_sql = [f"{self.measure_sql(b, alias)} AS {b}" for b in bases]
        month_cols = [*dims, "period_month", "toYear(period_month) * 12 + toMonth(period_month) AS month_no"]
        outer_where, having = self.metric_predicates(query, alias)
        if query.plan:
            source = self.plan_source(alias)
            inner_where = self.dimension_predicates(query, alias, wide, raw=True) + outer_where
        else:
            source = self.actual_source(alias, self.dimension_predicates(query, None, wide, raw=True), query.as_of)
            inner_where = [f"{alias}._is_deleted = 0", *outer_where]
        layer1 = f"SELECT {', '.join(month_cols + base_sql)} FROM {source}"
        if inner_where:
            layer1 += " WHERE " + " AND ".join(inner_where)
        layer1 += " GROUP BY " + ", ".join([*dims, "period_month"])
        if having:
            layer1 += " HAVING " + self.combine_predicates(having, query)

        # Layer 2: windows over the monthly series.
        windowed = [
            f"{self.window_sql(m.name, dims)} AS {m.alias or self.default_alias(m.name)}"
            for m in query.measures
        ]
        layer2 = f"SELECT {', '.join([*dims, 'period_month', *windowed])} FROM ({layer1})"

        # Layer 3: only the months that were asked for.
        sql = (
            f"SELECT * FROM ({layer2}) WHERE period_month >= toDate({self.bind(start, 'String')}) "
            f"AND period_month < toDate({self.bind(end, 'String')}) "
            f"ORDER BY {', '.join([*dims, 'period_month'])}"
        )
        if query.limit is not None:
            sql += f" LIMIT {query.limit}"
        return sql

    def window_sql(self, value: str | TimeFunction | Aggregate, dims: list[str]) -> str:
        """One time function over the monthly series produced by layer 1.

        RANGE frames over ``month_no`` select by *month distance*, so a lag of
        twelve is the same calendar month last year even when a month in
        between is missing from the group. A ROWS frame would quietly hand
        back whatever row happened to be twelve rows back.
        """
        if isinstance(value, Aggregate):
            return value.metric
        if not isinstance(value, TimeFunction):
            return value
        base = self.base_metric(value)

        def over(*extra_keys: str) -> str:
            keys = [*dims, *extra_keys]
            partition = f"PARTITION BY {', '.join(keys)} " if keys else ""
            return f"OVER ({partition}ORDER BY month_no"

        if value.name in {"PRIOR", "LEAD"}:
            offset = int(value.offset or 1)
            frame = f"{offset} PRECEDING AND {offset} PRECEDING" if value.name == "PRIOR" else f"{offset} FOLLOWING AND {offset} FOLLOWING"
            return f"anyOrNull({base}) {over()} RANGE BETWEEN {frame})"
        if value.name == "YOY":
            return f"{base} / nullIf(anyOrNull({base}) {over()} RANGE BETWEEN 12 PRECEDING AND 12 PRECEDING), 0) - 1"
        if value.name == "CAGR":
            years = int(value.offset or 1)
            months = 12 * years
            prior = f"anyOrNull({base}) {over()} RANGE BETWEEN {months} PRECEDING AND {months} PRECEDING)"
            return f"pow({base} / nullIf({prior}, 0), 1 / {years}) - 1"
        if value.name == "ROLLING":
            window = int(value.offset or 3)
            return f"sum({base}) {over()} RANGE BETWEEN {window - 1} PRECEDING AND CURRENT ROW)"
        if value.name == "YTD":
            return f"sum({base}) {over('toYear(period_month)')} RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        if value.name == "QTD":
            return f"sum({base}) {over('toStartOfQuarter(period_month)')} RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        if value.name == "MTD":
            return base
        raise DSLValidationError(f"unsupported time function: {value.name}")

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------
    def dimension_predicates(self, query: Query, alias: str | None, period: Period | None, raw: bool = False) -> list[str]:
        """Everything that can be decided per row: dimensions, period, scope, plan."""
        where = self.user_dimension_predicates(query, alias)
        if period:
            where.append(self.period_sql(period, alias, raw=raw))
        where.extend(self.scope_predicates(alias))
        if query.plan and not query.bridge:
            prefix = f"{alias}." if alias else ""
            where.append(f"{prefix}plan_version = {self.bind(query.plan.version, 'String')}")
            where.append(f"{prefix}scenario_id = {self.bind(query.plan.scenario, 'String')}")
        return where

    def user_dimension_predicates(self, query: Query, alias: str | None) -> list[str]:
        """The WHERE clause's dimension filters as one predicate, AND/OR kept.

        Validation refuses an OR between a dimension and a measure, so when the
        two are mixed every connector is AND and the positions still line up.
        """
        parts = [self.comparison_sql(c, alias) for c in query.predicates if c.field in self.schema.dimensions]
        return [self.combine_predicates(parts, query)] if parts else []

    def scope_predicates(self, alias: str | None) -> list[str]:
        # Scope is injected from authenticated context, never trusted from
        # model-generated DSL text.
        if self.security_context.allowed_companies is None:
            return []
        if not self.security_context.allowed_companies:
            return ["0 = 1"]
        values = tuple(Literal(c, True) for c in sorted(self.security_context.allowed_companies))
        return [self.comparison_sql(Comparison("company", "IN", values), alias)]

    def metric_predicates(self, query: Query, alias: str) -> tuple[list[str], list[str]]:
        """Measures in WHERE become HAVING; nothing else lands here."""
        having = [
            self.comparison_sql(comparison, alias)
            for comparison in query.predicates
            if comparison.field in self.schema.metric_names
        ]
        return [], having

    def combine_predicates(self, predicates: list[str], query: Query) -> str:
        # Preserve the user's AND/OR connectors while parenthesizing each
        # combination so mixed predicates have deterministic precedence.
        result = predicates[0]
        for index, predicate in enumerate(predicates[1:]):
            connector = query.predicate_connectors[index] if index < len(query.predicate_connectors) else "AND"
            result = f"({result} {connector} {predicate})"
        return result

    def comparison_sql(self, comparison: Comparison, alias: str | None) -> str:
        # Measures become expressions in HAVING; dimensions remain qualified
        # columns in WHERE. An alias of None means the unqualified column,
        # for use inside the vintage subquery.
        field = f"{alias}.{comparison.field}" if alias else comparison.field
        if comparison.field in self.schema.metric_names:
            field = self.measure_sql(comparison.field, alias or "")
        if comparison.operator in {"IN", "NOT IN"}:
            values = ", ".join(self.literal_sql(v) for v in comparison.values)
            return f"{field} {comparison.operator} ({values})"
        return f"{field} {comparison.operator} {self.literal_sql(comparison.values[0])}"

    def period_sql(self, period: Period, alias: str | None, raw: bool = False) -> str:
        # Use >= start and < end for every period, including ranges. `raw`
        # means the bounds are already YYYY-MM-DD dates rather than period
        # tokens (the widened range of a monthly query).
        start = period.start if raw else self.period_start(period.start)
        end = period.end if raw else self.period_end(period.end)
        column = f"{alias}.period_month" if alias else "period_month"
        return (
            f"{column} >= toDate({self.bind(start, 'String')}) "
            f"AND {column} < toDate({self.bind(end, 'String')})"
        )

    @staticmethod
    def period_start(period: str) -> str:
        # Normalize year, quarter, half-year, and month tokens to month starts.
        year = int(period[:4])
        if len(period) == 4:
            return f"{year:04d}-01-01"
        suffix = period[5:]
        month = ((int(suffix[1]) - 1) * 3 + 1 if suffix.startswith("Q")
                 else (int(suffix[1]) - 1) * 6 + 1 if suffix.startswith("H")
                 else int(suffix))
        return f"{year:04d}-{month:02d}-01"

    @classmethod
    def period_end(cls, period: str) -> str:
        # Return the first day after the requested period for half-open SQL.
        year = int(period[:4])
        if len(period) == 4:
            return f"{year + 1:04d}-01-01"
        suffix = period[5:]
        month = (int(suffix[1]) * 3 + 1 if suffix.startswith("Q")
                 else int(suffix[1]) * 6 + 1 if suffix.startswith("H")
                 else int(suffix) + 1)
        return f"{year + 1:04d}-01-01" if month == 13 else f"{year:04d}-{month:02d}-01"

    @staticmethod
    def add_months(date: str, months: int) -> str:
        year, month = int(date[:4]), int(date[5:7])
        index = year * 12 + (month - 1) + months
        return f"{index // 12:04d}-{index % 12 + 1:02d}-01"

    # ------------------------------------------------------------------
    # Measures
    # ------------------------------------------------------------------
    def measure_sql(self, value: str | TimeFunction | Aggregate, alias: str) -> str:
        if isinstance(value, TimeFunction):
            # Only reachable from compile_plain when validation let a time
            # function through, which it does not; monthly queries go through
            # window_sql. Kept explicit rather than silently degrading.
            raise DSLValidationError(f"{value.name} must be compiled as a monthly series")
        if isinstance(value, Aggregate):
            value = value.metric
        expression = self.schema.metrics[value]["sql_expression"]
        return expression.replace("{a}.", f"{alias}." if alias else "").replace("{a}", alias)

    @staticmethod
    def default_alias(value: str | TimeFunction | Aggregate) -> str:
        if isinstance(value, TimeFunction):
            return f"{value.name.lower()}_{Compiler.base_metric(value)}"
        if isinstance(value, Aggregate):
            return value.metric
        return value

    # ------------------------------------------------------------------
    # Bridge: matched plan/actual lines for the bridge module
    # ------------------------------------------------------------------
    def compile_bridge(self, query: Query) -> CompiledQuery:
        """Matched plan and actual lines, with both FX rates, at signature grain.

        The arithmetic is not done here. The bridge is a nested decomposition
        that has to tie at every level of a rollup, and that is a job for
        ``dsl.bridge`` in Python where it can be tested as an identity. This
        query's job is to fetch exactly the lines it runs over: one row per
        matched (company, month, account, signature), plan quantity and price,
        actual quantity and price, the assumed rate and the real rate.

        Intercompany trade is eliminated: the report is a group view in USD,
        and a sale from one entity to another is not revenue to the group.
        Both halves of a pair carry ``intercompany_flag = 'Yes'`` on the same
        signature (the sale, and its mirrored 51500 cost on the buyer), so
        dropping the flagged rows drops whole pairs. Nothing is netted in
        functional currency, so this is not eliminating before translating.
        """
        assert query.plan is not None
        accounts: set[str] = set()
        for measure in query.measures:
            base = self.base_metric(measure.name)
            metric_accounts = self.schema.metrics[base].get("accounts")
            if not metric_accounts:
                raise DSLValidationError(
                    f"{base} cannot be bridged: it is not an additive measure over ledger accounts"
                )
            accounts.update(metric_accounts)
        account_list = ", ".join(self.bind(a, "String") for a in sorted(accounts))

        inner = [
            self.period_sql(query.period, None) if query.period else None,
            f"account IN ({account_list})",
            f"intercompany_flag != {self.bind('Yes', 'String')}",
            *self.scope_predicates(None),
            *self.user_dimension_predicates(query, None),
        ]
        inner = [p for p in inner if p]
        dims = [d for d in query.dimensions if d not in ACTUAL_KEY]
        actual = self.actual_rollup("a", inner, query.as_of, dims)
        dim_cols = "".join(f"a.{d}, " for d in dims)
        plan_version = self.bind(query.plan.version, "String")
        scenario = self.bind(query.plan.scenario, "String")
        join_keys = " AND ".join(f"a.{key} = p.{key}" for key in ACTUAL_KEY)
        sql = (
            f"SELECT a.company, a.period_month, a.account, d.account_type AS account_type, a.functional_currency, {dim_cols}"
            "a.dim_signature_hash, "
            "p.quantity AS plan_quantity, p.unit_price AS plan_unit_price, p.amount_functional AS plan_amount, "
            "a.quantity AS actual_quantity, a.unit_price AS actual_unit_price, a.amount_functional AS actual_amount, "
            "fp.rate AS plan_fx, fa.rate AS actual_fx "
            f"FROM {actual} "
            f"INNER JOIN (SELECT * FROM fpa_cube.fact_plan_line FINAL WHERE plan_version = {plan_version} "
            f"AND scenario_id = {scenario}) AS p ON {join_keys} "
            "INNER JOIN fpa_cube.dim_account AS d ON d.account = a.account "
            f"INNER JOIN (SELECT period_month, from_currency, rate FROM fpa_cube.dim_fx_plan "
            f"WHERE plan_version = {plan_version}) AS fp "
            "ON fp.period_month = a.period_month AND fp.from_currency = a.functional_currency "
            "INNER JOIN fpa_cube.dim_fx_actual AS fa "
            "ON fa.period_month = a.period_month AND fa.from_currency = a.functional_currency "
            f"ORDER BY a.company, a.period_month, a.account, {''.join(f'a.{d}, ' for d in dims)}a.dim_signature_hash"
        )
        estimated = self.estimate_rows(query)
        self.enforce_budget(estimated)
        return CompiledQuery(sql, self.params, estimated, query.as_of or "current", bridge=True)

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------
    def estimate_rows(self, query: Query) -> int:
        # A conservative guardrail used before execution, not a replacement
        # for ClickHouse's planner. The bridge runs over the matched set,
        # which is a quarter the size of the plan.
        base = 65_000 if query.bridge else 231_390 if query.plan else 1_034_766
        # The plan covers 12 months and the ledger 24; a period predicate
        # reads its share of them. Time functions widen the read, which is
        # still a fraction of the table and is not counted twice here.
        if query.period:
            start, end = self.period_start(query.period.start), self.period_end(query.period.end)
            months = (int(end[:4]) * 12 + int(end[5:7])) - (int(start[:4]) * 12 + int(start[5:7]))
            base = base * max(1, months) // (12 if query.plan or query.bridge else 24)
        if query.dimensions and not query.bridge:
            base //= max(1, 2 ** min(len(query.dimensions), 5))
        if query.limit is not None:
            base = min(base, query.limit)
        return max(1, base)

    def enforce_budget(self, estimated: int) -> None:
        if estimated > self.security_context.max_estimated_rows:
            raise DSLValidationError(
                f"query exceeds estimated row budget: {estimated} > {self.security_context.max_estimated_rows}"
            )

    # ------------------------------------------------------------------
    # Literals
    # ------------------------------------------------------------------
    def literal_sql(self, literal: Literal) -> str:
        # Keep literal values in the parameter map so they cannot alter SQL
        # structure or identifier resolution.
        return self.bind(literal.value, "String" if literal.is_string else "Decimal64(6)")

    @staticmethod
    def is_safe_identifier(value: str) -> bool:
        """Allow only parser-compatible aliases before placing them in SQL text."""
        return bool(value) and value[0].isalpha() and all(
            character.isalnum() or character == "_" for character in value
        )

    def bind(self, value: Any, type_name: str) -> str:
        # Generate stable named placeholders and retain their typed values for
        # the ClickHouse client.
        name = f"p{self.param_index}"
        self.param_index += 1
        self.params[name] = value
        return "{" + name + ":" + type_name + "}"


def compile_query(
    source: str,
    schema: Schema | None = None,
    security_context: SecurityContext | None = None,
) -> CompiledQuery:
    return Compiler(schema, security_context).compile(parse_query(source))


def clickhouse_datetime(value: str) -> str:
    """An ISO timestamp as ClickHouse will accept it for a ``DateTime`` param.

    ClickHouse parses a bound ``DateTime`` whole and refuses anything left
    over, so an offset ("2026-08-12T09:30:00+00:00") is rejected outright with
    BAD_QUERY_PARAMETER. A close read back from Postgres is offset-aware, so
    the drill-through's vintage pin arrives in exactly that shape. Converting
    to UTC keeps the instant identical; only the spelling changes.
    """
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        # Not a timestamp this can normalize; leave it for ClickHouse to judge.
        return text
    if moment.tzinfo is None:
        # Already in the shape ClickHouse parses; keep it exactly as written.
        return text
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def compile_citation_rows(keys: list[dict[str, Any]], closed_at: str, scope: SecurityContext) -> CompiledQuery:
    """Bounded, vintage-pinned ledger lookup for authorized persisted citations."""
    if not scope.allowed_companies:
        raise DSLValidationError("citation lookup requires an explicit entity scope")
    if not keys or len(keys) > 200:
        raise DSLValidationError("citation lookup requires between 1 and 200 keys")
    compiler = Compiler(security_context=scope)
    predicates = []
    for key in keys:
        if key["company_code"] not in scope.allowed_companies:
            raise DSLValidationError("citation company outside caller scope")
        fields = [("company", key["company_code"]), ("period_month", str(key["period_month"])),
                  ("account", key["account_code"]), ("dim_signature_hash", key["dim_signature_hash"])]
        predicates.append("(" + " AND ".join(f"{name} = " +
            (f"toDate({compiler.bind(value, 'String')})" if name == "period_month" else compiler.bind(value, "String"))
            for name, value in fields) + ")")
    source = compiler.actual_source("a", ["(" + " OR ".join(predicates) + ")"], closed_at)
    return CompiledQuery(f"SELECT a.* FROM {source} WHERE a._is_deleted = 0 ORDER BY company, period_month, account, dim_signature_hash",
                         compiler.params, len(keys), closed_at)
