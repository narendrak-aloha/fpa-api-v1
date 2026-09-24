"""Optional ratio operands from the same scoped aggregate query.

Extra columns are removed before returning the original result table. No
second query, changed grouping, or inferred source values are involved.
"""
from decimal import Decimal

from fpa_project.dsl.ast import Query
from fpa_project.dsl.compiler import Compiler

RATIOS = {
    "utilisation": ("Service quantity (accounts 41000, 41010, 41020)", "Delivery quantity (accounts 51000, 51050, 51400)"),
    "realisation": ("Service amount (accounts 41000, 41010, 41020)", "Service quantity (accounts 41000, 41010, 41020)"),
    "gross_margin_pct": ("Gross margin", "Services revenue"),
}


def ratio_projection(sql: str, query: Query, schema) -> tuple[str, list[dict]]:
    if query.bridge or any(not isinstance(m.name, str) for m in query.measures):
        return sql, []
    descriptors, extra = [], []
    alias = "p" if query.plan else "a"
    output_names = set(query.dimensions) | {m.alias or Compiler.default_alias(m.name) for m in query.measures}
    for measure in query.measures:
        metric = measure.name
        if metric not in RATIOS:
            continue
        expression = schema.metrics[metric]["sql_expression"].replace("{a}", alias)
        if " / nullIf(" not in expression or not expression.endswith(", 0)"):
            continue
        numerator, denominator = expression.split(" / nullIf(", 1)
        denominator = denominator[:-4]
        prefix = f"__calculation_{len(descriptors)}"
        keys = [prefix + "_numerator", prefix + "_denominator"]
        if any(k in output_names for k in keys):
            return sql, []  # An explicit alias must never be overwritten.
        extra.extend([f"{numerator} AS {keys[0]}", f"{denominator} AS {keys[1]}"])
        descriptors.append({"metric": metric, "column": measure.alias or Compiler.default_alias(metric), "keys": keys})
    if not extra:
        return sql, []
    select, source = sql.split(" FROM ", 1)
    return select + ", " + ", ".join(extra) + " FROM " + source, descriptors


def extract_ratio_calculations(row: dict, descriptors: list[dict]) -> list[dict]:
    calculations = []
    for descriptor in descriptors:
        keys = descriptor["keys"]
        operands = [row.pop(k, None) for k in keys]
        if any(v is None for v in operands):
            continue
        metric, column = descriptor["metric"], descriptor["column"]
        labels = RATIOS[metric]
        result = row.get(column)
        calculations.append({
            "label": column.replace("_", " ").capitalize(),
            "formula": f"{labels[0]} ÷ {labels[1]}",
            "inputs": dict(zip(labels, (str(v) for v in operands))),
            "substitution": f"{operands[0]} ÷ {operands[1]}" if Decimal(str(operands[1])) != 0 else None,
            "result": str(result) if result is not None else None,
            "unit": "", "notes": [
                "Calculated from totals for this row’s group using the same filters as the answer, not an average of individual ratios.",
                "The denominator is zero; the ratio is undefined." if Decimal(str(operands[1])) == 0 else
                ("The result is a fraction: 0.75 means 75%." if metric != "realisation" else "Amounts are in local currency."),
            ],
        })
    return calculations
