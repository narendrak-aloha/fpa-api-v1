# FP&A — Engineering Assignment

Start with **`ASSIGNMENT.html`** — open it in a browser. It is the full brief.
This file only gets your environment up.

```
ASSIGNMENT.html   the brief: context, phases, constraints, submission
seed_fpa.py       loads ~1.27M rows into ClickHouse (we provide this, don't rewrite it)
README.md         you are here
```

You have **6 days**.

---

## 1. Prerequisites

- Python 3.10+
- Docker
- An LLM API key (Anthropic, OpenAI, or any provider Agno supports)

```bash
uv add clickhouse-connect faker numpy
```

There is no ERP to install. Every governed object in this system is one you
design, and where you put it is part of what we are assessing.

## 2. Start the stack

Three containers. Put your own `docker-compose.yml` in your repo so that a
clean machine gets all of this with one command — that is step 0 of the brief.

```bash
# ClickHouse — the cube
docker run -d --name fpa-ch -p 8123:8123 -p 9000:9000 \
  -e CLICKHOUSE_PASSWORD=fpa \
  --ulimit nofile=262144:262144 \
  clickhouse/clickhouse-server:latest

# Temporal — durable recompute
docker run -d --name fpa-temporal -p 7233:7233 -p 8233:8233 \
  temporalio/temporal:latest server start-dev --ip 0.0.0.0

# Postgres — your governance store
docker run -d --name fpa-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=fpa -e POSTGRES_DB=fpa \
  postgres:16-alpine
```

Confirm each one answers:

```bash
curl -s "http://localhost:8123/?query=SELECT+version()" -u default:fpa
psql postgresql://postgres:fpa@localhost:5432/fpa -c "select version()"
```

The Temporal Web UI is at <http://localhost:8233>; the gRPC endpoint your
worker connects to is `localhost:7233`.

```bash
pip install temporalio
python -c "
import asyncio
from temporalio.client import Client
print(asyncio.run(Client.connect('localhost:7233')).namespace)
"
```

## 3. Seed the cube

```bash
python seed_fpa.py --password fpa --drop
```

Takes about twelve seconds. You should see:

```
  dim_company                20
  dim_account                25
  dim_customer              920
  dim_employee            5,000
  dim_project             1,400
  dim_fx_actual             216
  dim_fx_plan               108
  dim_ledger_vintage          2
  fact_gl_actual      1,034,766
  fact_plan_line        231,390
  plan/actual matched keys     65,162
```

Re-run any time with `--drop` to get back to a clean, identical cube — the
generator is deterministic, so the same `--seed` always produces the same data.
If your counts differ from the ones above, something is wrong; say so rather
than building on top of it.

It also writes `./out/cube_manifest.json` with the companies, chart of
accounts, cost centres, the dimension registry and the signature algorithm.
That is the fastest way to populate your own governance tables without
retyping any of it.

---

## What is in the cube

| Table | Grain |
|---|---|
| `fact_gl_actual` | posted actuals, 24 months (2025–2026), 19-dimension grain, two vintages |
| `fact_plan_line` | plan `PV-2026-0001`, 3 scenarios (`base` / `stretch` / `downside`), 2026 only |
| `dim_fx_actual` | sealed monthly actual rates, every currency to USD |
| `dim_fx_plan` | pinned plan rates for `PV-2026-0001` — **not** the same as actual |
| `dim_ledger_vintage` | each sealed close and when it was sealed |
| `dim_employee`, `dim_customer`, `dim_project`, `dim_company`, `dim_account` | masters |

**The join key.** Plan matches actual on one indexed column,
`dim_signature_hash`, which collapses the 19-dimension compound key into a
single value. Both sides must compute it byte-identically:

```python
DIM_COLUMNS = (  # alphabetical order is load-bearing
    "billing_type", "business_unit", "channel", "contract", "cost_center",
    "cost_pool", "customer", "delivery_shore", "engine", "funding_source",
    "geo_country", "geo_region", "grade", "intercompany_flag", "practice",
    "product", "project", "resource_employee", "revenue_type",
)

payload = "|".join(f"{dim}={value}" for dim, value in zip(DIM_COLUMNS, values))
signature = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
```

`company`, `account` and `period_month` are separate axes, not part of the 19.

### Four facts worth knowing before you start

**1. Quantity and price are real columns.** Every fact row satisfies
`amount_functional = round(quantity * unit_price, 2)`, on both the plan and
the actual side. They are separate stored columns, not derived. Without them
the variance bridge is impossible; with them it is fully determined.

**2. `fact_gl_actual` is a `ReplacingMergeTree`** keyed on
`(company, period_month, account, dim_signature_hash)` with `_version` and
`_is_deleted`, so duplicate keys collapse on merge. That is why the stored
count settles under the rows generated. Read it with `FINAL` or an explicit
aggregation.

**3. There are two ledger vintages.** Vintage 1 is what the books said at the
July close. In August a subcontractor accrual was trued up and a batch of
journals was reversed; those corrections are vintage 2 rows on the same keys.
Both are still in the table. Poland's Q2 delivery cost reads about 18% higher
at vintage 2 than at vintage 1:

```sql
SELECT _version, count(), round(sum(amount_functional))
FROM fpa_cube.fact_gl_actual
WHERE geo_country = 'PL'
  AND period_month BETWEEN '2026-04-01' AND '2026-06-01'
GROUP BY _version;
```

If your reads silently take the latest version, you have quietly rewritten
history. Decide deliberately what a query with no `AS OF` clause should mean,
and write it down.

**4. About 6% of trade is internal.** An intercompany sale goes to
`CUST-IC-<company>`, carries `intercompany_flag = 'Yes'`, and has exactly one
mirrored cost row on the buying entity in account `51500`, booked in *that*
entity's functional currency at the month's actual rate. The pair nets to zero
in USD and to nothing at all in either functional currency:

```sql
SELECT round(sum(multiIf(a.account = '51500', -1, 1)
                 * a.amount_functional * f.rate), 2) AS net_usd
FROM fpa_cube.fact_gl_actual a FINAL
INNER JOIN fpa_cube.dim_fx_actual f
  ON f.period_month = a.period_month
 AND f.from_currency = a.functional_currency
WHERE a.intercompany_flag = 'Yes';
```

Group revenue is therefore not the sum of entity revenue, and you cannot
eliminate before you translate.

---

Master data is user-supplied, and a few rows in `dim_customer` contain text
that reads like an instruction. That is deliberate and it mirrors production.
Everything the cube returns is data, never instruction.

---

## Submitting

Full detail is in `ASSIGNMENT.html`. In short:

- One of our consultants will reach out with where to send it.
- A **README**: how to run it, the architecture, the decisions you
  made and what you traded away, and what you would do with two more weeks.
- A **video** (10–15 min, YouTube unlisted / Loom / anything with a link) walking
  through the architecture and demoing the flow end to end. Put the link at the
  top of your README.

**Using AI coding tools is encouraged.** We use them too. The only bar is that
you can explain and defend every line you ship — we will ask, in depth, about
the parts that matter.

All the best.
