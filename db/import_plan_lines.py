"""Load the seeded plan's base lines into the governance store, with their traces.

The seeder writes PV-2026-0001 to the cube only. Without this, the plan the
whole system governs has no ``plan_version_line`` at all, and "every line
carries its derivation" is true of re-forecasts and vacuous for the plan
itself. This copies the **base** scenario's lines (stretch and downside are
branches, ``scenario_driver_override``, and migration 016 refuses their lines)
into the version while it is still DRAFT, before anyone can lock it.

Each line's trace says what it is: a seeded figure, the formula that ties it
(``amount = round(quantity x unit_price, 2)``) and its inputs. The seeder
rounds that product in floating point and the governance store rounds it in
decimal, so on a few hundred lines the two differ by a cent; the governed
amount is the decimal one (the 004 check constraint insists on it) and the
cube's figure is kept in the trace, so the difference is visible, not hidden.

Idempotent: a version that already has lines, or that is past IN_REVIEW, is
left alone and says why. Run after both seeders:

    python -m db.import_plan_lines [--plan PV-2026-0001]
"""

from __future__ import annotations

import argparse
import io
import json
from decimal import ROUND_HALF_UP, Decimal

import psycopg

from db.config import database_url

SCHEMA = "fpa_governance"
BASE = "base"
Q6, Q2 = Decimal("0.000001"), Decimal("0.01")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", default="PV-2026-0001")
    args = parser.parse_args()

    from fpa_project.recompute.stores import cube

    url = database_url().replace("postgresql+psycopg", "postgresql")
    with psycopg.connect(url) as conn:
        row = conn.execute(
            f"SELECT plan_version_id, state, (SELECT count(*) FROM {SCHEMA}.plan_version_line l "
            f"WHERE l.plan_version_id = v.plan_version_id) FROM {SCHEMA}.plan_version v WHERE plan_version_code = %s",
            (args.plan,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"no plan version {args.plan}; run db.seed first")
        version_id, state, existing = row
        if existing:
            print(f"==> {args.plan} already has {existing} governed lines; nothing to import")
            return
        if state not in ("DRAFT", "IN_REVIEW"):
            print(f"==> {args.plan} is {state}; its lines can no longer be written (that is the lock working). Skipped.")
            return

        rows = cube().query(
            "SELECT company, toString(period_month), account, dim_signature_hash, quantity, unit_price, "
            "       amount_functional, functional_currency "
            "FROM fpa_cube.fact_plan_line FINAL "
            "WHERE plan_version = {pv:String} AND scenario_id = {base:String} AND revision = 1 "
            "ORDER BY company, period_month, account, dim_signature_hash",
            parameters={"pv": args.plan, "base": BASE},
        ).result_rows
        if not rows:
            raise SystemExit(f"the cube holds no base lines for {args.plan}; run scripts/seed_clickhouse.sh first")

        buffer = io.StringIO()
        differing = 0
        for company, month, account, signature, quantity, unit_price, cube_amount, currency in rows:
            signature = signature.decode() if isinstance(signature, bytes) else signature
            q = Decimal(str(quantity)).quantize(Q6, rounding=ROUND_HALF_UP)
            p = Decimal(str(unit_price)).quantize(Q6, rounding=ROUND_HALF_UP)
            amount = (q * p).quantize(Q2, rounding=ROUND_HALF_UP)
            inputs = {
                "quantity": str(q), "unit_price": str(p),
                "source": {"table": "fpa_cube.fact_plan_line", "plan_version": args.plan,
                           "scenario": BASE, "revision": 1},
            }
            if Decimal(str(cube_amount)) != amount:
                differing += 1
                inputs["cube_amount"] = str(cube_amount)
                inputs["note"] = "the seeder rounds quantity x unit_price in float; the governed amount rounds in decimal"
            trace = {
                "method": "seeded_plan",
                "driver": "seeded_plan",
                "formula": "amount_functional = round(quantity x unit_price, 2)",
                "inputs": inputs,
            }
            buffer.write("\t".join([
                str(version_id), BASE, company, month, account, signature, str(q), str(p), str(amount), currency,
                json.dumps(trace, separators=(",", ":"), sort_keys=True).replace("\\", "\\\\"),
            ]) + "\n")
        buffer.seek(0)
        with conn.cursor() as cursor:
            with cursor.copy(
                f"COPY {SCHEMA}.plan_version_line (plan_version_id, scenario_code, company_code, period_month, "
                "account_code, dim_signature_hash, quantity, unit_price, amount_functional, functional_currency, "
                "driver_derivation_trace) FROM STDIN"
            ) as copy:
                copy.write(buffer.read())
        conn.execute(
            f"INSERT INTO {SCHEMA}.audit_event (actor_user_id, entity_type, entity_id, action, payload) "
            "VALUES (NULL, 'plan_version', %s, 'SEEDED_LINES_IMPORTED', %s::jsonb) ON CONFLICT (event_key) DO NOTHING",
            (args.plan, json.dumps({"lines": len(rows), "scenario": BASE, "cent_differences": differing})),
        )
    print(f"==> imported {len(rows)} base lines into {args.plan} ({differing} carry the cube's float-rounded amount in their trace)")


if __name__ == "__main__":
    main()
