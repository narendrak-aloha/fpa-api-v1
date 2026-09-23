# DSL module

The module has four stages:

1. `lexer.py` tokenizes keywords, identifiers, numbers, strings, operators,
   and period range markers without evaluating user input.
2. `parser.py` builds the typed dataclasses in `ast.py`. Query clauses are
   order-sensitive, matching the documented DSL grammar.
3. `schema.py` loads `data/schema_snapshot.json` and validates dimensions,
   measures, and scenarios against the seed-derived contract.
4. `compiler.py` maps validated measures to ClickHouse expressions and emits a
   `CompiledQuery(sql, params)` object.

The complete agreed EBNF is stored beside the implementation in
`grammar.ebnf`.

## API

```python
from fpa_project.dsl import compile_query

result = compile_query(
    "SELECT services_revenue BY geo_country "
    "WHERE geo_country = 'PL' FOR PERIOD 2026-Q2"
)
print(result.sql)
print(result.params)
```

The output uses ClickHouse named-parameter syntax, for example
`{p0:String}`. Parameter values are kept in `result.params`, so a value cannot
change the SQL structure. `ParseError` indicates malformed DSL; `DSLValidationError`
indicates a well-formed query that violates the schema or semantic contract.

## Deliberate constraints

- Query dimensions are checked against the 19 planning dimensions plus the
  seed's separate `company` and `account` axes.
- `IN` and `NOT IN` require one or more literals.
- `BRIDGE` requires a plan comparison.
- Periods support years, quarters, halves, months, and inclusive-looking
  ranges compiled as a half-open ClickHouse date interval.
- `AS OF` resolves an actuals vintage through `dim_ledger_vintage`.
- Financial ratios are expressions from their numerator and denominator, not
  aggregates of already-computed percentages.
- `SecurityContext` injects the caller's allowed companies and rejects queries
  above the caller's estimated-row budget.
- `SecurityContext` fails closed for invalid budgets or malformed company
  scopes. It is constructed by the authenticated application, never by the
  DSL or model output.
- Actual reads use ClickHouse `FINAL`; `AS OF` resolves the ledger vintage.
- `AS OF` on a plan query, reverse period ranges, and invalid measure aliases
  are rejected before SQL generation.
- Formula validation enforces function arity and rejects unknown references,
  illegal ratio aggregation, and true dependency cycles. Historical `PRIOR`
  references remain valid.
- `BRIDGE` compiles a matched plan/actual query over the seed's four-key join
  (`company`, `period_month`, `account`, `dim_signature_hash`) with price,
  volume, gap, and residual columns. The returned SQL is the foundation for
  the full rollup bridge; mix/FX decomposition and residual tolerance checks
  still belong in the bridge/reporting layer.
- `bridge.decompose()` provides the deterministic price/volume/mix/FX formula
  over matched rows and exposes the residual for tolerance assertions.

The compiler is intentionally a SQL generator, not a database client. An
application may execute its result with `clickhouse-connect` or another
ClickHouse driver after applying its own authorization and row-scope context.
