"""Deterministic vintage reconciliation; an agent cannot clear recorded drift."""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

from fpa_project.dsl.compiler import Compiler, vintage_lookup
from fpa_project.dsl.parser import parse_query
from fpa_project.governance import engine, record


def reconcile(dsl, left_as_of, right_as_of, scope, executor, actor):
    query = parse_query(dsl)
    sides = []
    for instant in (left_as_of, right_as_of):
        sql, params = vintage_lookup(instant)
        closes = list(executor(sql, params))
        if not closes:
            raise ValueError(f"no sealed ledger vintage at {instant}")
        compiled = Compiler(security_context=scope).compile(replace(query, as_of=instant))
        rows = list(executor(compiled.sql, compiled.params))
        totals = {}
        for row in rows:
            for key, value in row.items():
                if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, Decimal(0)) + Decimal(str(value))
        digest = hashlib.sha256(json.dumps(sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)).encode()).hexdigest()
        sides.append({"vintage": closes[0]["vintage"], "rows": len(rows), "digest": digest,
                      "totals": {k: str(v) for k, v in totals.items()}})
    changed = sides[0]["digest"] != sides[1]["digest"]
    deltas = {k: str(Decimal(sides[1]["totals"].get(k, "0")) - Decimal(sides[0]["totals"].get(k, "0")))
              for k in sides[0]["totals"].keys() | sides[1]["totals"].keys()}
    result = {"drift": changed, "left": sides[0], "right": sides[1], "deltas": deltas,
              "scope": sorted(scope.allowed_companies or [])}
    if changed:
        key = hashlib.sha256((dsl + left_as_of + right_as_of + json.dumps(result, sort_keys=True)).encode()).hexdigest()
        with engine().begin() as conn:
            record(conn, actor, "vintage_drift", key, "DRIFT_DETECTED", result)
    return result
