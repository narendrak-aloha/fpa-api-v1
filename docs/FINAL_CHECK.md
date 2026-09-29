Below is the exact Markdown content from above, ready to save as `FP&A_System_Requirements.md`. I have not changed the wording or structure.

 FP&A System — Evaluator Requirements, Issues, and Verification Checklist

# FP&A System — Issues, Requirements, and Verification Checklist

 ## 1\. Plan Locking and Database-Level Enforcement

 ### 1.1 Locked Through Interface

 What is the means by locked through interface?

 ### 1.2 Testing Our Own Created Plan Without Approving It

 How can we test our own created plan without approving it?

 ### 1.3 Lock Enforcement at DB Level

 Where we enforce the lock plan must be lock on the db level if he/she tries sql query on the DB or someone bypass the api and write put update the plan.

 Where we enforce I want to see:

```
UPDATE plan_version_line
SET amount = 500000
WHERE plan_version_id = 'PV-002';
```

---

 # 2\. Driver Derivation Trace

 ## 2.1 Current Driver Derivation Trace

 Currently its like these:

```
{
  "method": "driver_elasticity",
  "account": "41000",
  "drivers": {
    "utilisation [RTPL1,RTPL2,RTPL3 2026-07..2026-12]": {
      "ratio": 0.96,
      "scope": {
        "months": [
          "2026-07-01",
          "2026-08-01",
          "2026-09-01",
          "2026-10-01",
          "2026-11-01",
          "2026-12-01"
        ],
        "companies": [
          "RTPL1",
          "RTPL2",
          "RTPL3"
        ]
      },
      "shocked_directly": true
    },
    "billable_hours [RTPL1,RTPL2,RTPL3 2026-07..2026-12]": {
      "ratio": 0.96,
      "scope": {
        "months": [
          "2026-07-01",
          "2026-08-01",
          "2026-09-01",
          "2026-10-01",
          "2026-11-01",
          "2026-12-01"
        ],
        "companies": [
          "RTPL1",
          "RTPL2",
          "RTPL3"
        ]
      },
      "shocked_directly": false
    }
  },
  "applied_factors": {}
}
```

 ## 2.2 Required Driver Derivation Trace

 They want:

```
{
  "driver": "utilisation",
  "formula": "available_hours * utilisation",
  "inputs": {
    "available_hours": 10000,
    "utilisation": 0.75
  }
}
```

 Without derivation trace no any line can be saved.

---

 # 3\. Scenario Branches

 ## 3.1 Requirement

 Scenario branches must NOT duplicate plan rows.

 ### 2.6 Scenario branches must NOT duplicate plan rows

 You have:

 - Base
- Stretch
- Downside

 Suppose base utilisation is:

 - Base: 75%
- Stretch: 80%
- Downside: 70%

 Don't create three complete copies of all plan lines.

 Instead:

 - Base plan
- Stretch: utilisation override = 80%
- Downside: utilisation override = 70%

 The assignment describes scenarios as driver overrides, not copies.

 ## 3.2 Current Implementation Question

 Currently I create each scenario row as plan line what they want do we create only one scenario set that is base which is what it ask and create only that not stretched and downside.

---

 # 4\. Evaluation

 They dont want eval means what.

---

 # 5\. Audit Chain and Tamper Detection

 ## 5.1 Requirement

 Audit chain must detect tempering.

 Suppose audit records are:

```
Event 1 → hash A
Event 2 → hash B
Event 3 → hash C
```

 Each event hashes the previous event.

 Someone changes Event 2.

 Now:

```
Event 1 → OK
Event 2 → CHANGED
Event 3 → hash no longer matches
```

 Your verifier should say:

```
AUDIT CHAIN INVALID
```

 They specifically want you to demonstrate that changing one historical audit row causes the verifier to fail.

---

 # 6\. Country-Wise Restriction

 ## 6.1 Requirement

 IS Country wise restricted user created and it really enforce when they ask for all countries.

---

 # 7\. Variance Report

 ## 7.1 Volume + Mix = Total Quantity Variance

 ### 4.4 Volume + mix = total quantity variance

 Suppose:

 - Plan quantity = 100,000 hours
- Actual quantity = 90,000 hours
- Quantity variance: -10,000

 Your decomposition should satisfy:

```
volume + mix ≈ -10,000
```

 within the defined tolerance.

 The assignment explicitly tests this identity.

---

 ## 7.2 Practice Mix and Grade-Within-Practice Mix Must Be Separate

 ### 4.5 Practice mix and grade-within-practice mix must be separate

 This is one of the traps.

 Suppose:

 Practice:

 - Data Platform
- Cloud Migration
- ERP

 And within Data Platform:

 - Analyst
- Consultant
- Senior Manager

 You need to distinguish:

 - Practice mix
- Grade mix within practice

 And both must actually contribute something.

 They don't want:

```
Practice mix = 0
Grade mix = 0
```

 just because your calculation was too simplistic.

---

 ## 7.3 Variance Report Storage

 Did we store the variance report actually tell me for the specific plan?

---

 # 8\. Temporal Workflow

 ## 8.1 Kill Worker Halfway Through Temporal Start

 How we can test he kill worker halfway when temporal start?

---

 ## 8.2 Concurrent Shock Strategy

 When one shock already running and planner create another what is our strategy according to the code tell me?

 Did we kill the second shock during first running only allow one at a time what was our strategy?

---

 ## 8.3 Replay Test

 Is replay test written for temporal workflow that satisfy point `5.10 Replay test`?

---

 # 9\. Agent Data Access Architecture

 ## 9.1 Required Data Access Path

 In our code implementation did we check agent will not direct access of psql and cube instead it follow:

```
Agent
 ↓
run_finops_query(DSL)
 ↓
Parser
 ↓
Compiler
 ↓
ClickHouse
```

---

 # 10\. PII Classification

 ## 10.1 Classification Failure

 I didnt verify the PII classification failed then what happened?

 Suppose your PII classifier crashes.

 Wrong:

```
Classification failed
 ↓
"Let's send it anyway"
```

 Correct:

```
Classification failed
 ↓
BLOCK
 ↓
Do not send data
```

 The assignment explicitly says the gate must fail closed.

---

 # 11\. Agent Cannot Invent Numbers

 ## 11.1 Requirement

 ### 6.5 Agent cannot invent numbers

 Suppose ClickHouse returns:

```
Revenue = 11.4M
Price variance = -0.5M
Volume variance = -0.8M
```

 Agent says:

 > "Revenue fell by 11.4M."

 That's wrong.

 The number 11.4M exists, but the statement is wrong because it changed the meaning.

 Or suppose it says:

 > "FX caused -0.7M."

 when the data says:

```
FX = -0.5M
```

 The output guardrail must catch that.

 The assignment requires a post-hook checking that every figure in the narrative appears in the cited result set.

---

 # 12\. Drill Through to Cube Rows

 ## 12.1 Requirement

 I am not sure is our system have the below functionality or not.

 ### 7.3 Drill through to cube rows Click

 `Volume -0.80M` and show:

```
Underlying cube rows
--------------------
company
period
account
practice
grade
quantity
unit_price
amount
vintage
...
```

 So the evaluator can answer:

 > "Where did this number come from?"

 The assignment explicitly requires cited cube rows and vintage.

---

 ## 12.2 Why Drill Through Is Required

 It means the evaluator wants traceability.

 Right now, your result might say:

```
Volume = -800,000
```

 The evaluator then asks:

 > "Okay, but where did this -800,000 come from?"

 Your system should let them click on:

```
Volume -0.80M
```

 and see the actual underlying ClickHouse/cube rows that were used to calculate that number.

 Simple example.

 Your report shows:

```
Company: RTAE1

Gap       -108,023
Price      -45,416
Volume     +94,827
Mix        -44,750
FX        -112,685
```

 The evaluator clicks:

```
Volume +94,827
```

 Your application should open something like:

```
Underlying cube rows

Company | Period    | Account | Practice       | Grade      | Quantity | Unit Price | Amount | Vintage
--------------------------------------------------------------------------------------------------------
RTAE1   | 2026-04   | 41000   | Data Platform  | Analyst    | 1,200    | 100.00     | ...    | 2
RTAE1   | 2026-04   | 41000   | Data Platform  | Consultant | 800      | 150.00     | ...    | 2
RTAE1   | 2026-05   | 41000   | Cloud          | Analyst    | 1,500    | 100.00     | ...    | 2
...
```

 These are the actual source rows from your cube/table.

---

 ## 12.3 Why Do They Want This?

 Because they don't want your application to simply say:

```
Volume = +94,827
```

 without being able to explain where that number came from.

 They want:

```
Volume +94,827
        ↓ click
Underlying rows
        ↓
These rows produced +94,827
```

 So the evaluator can independently trace:

```
Source rows
 ↓
calculation
 ↓
Volume
 ↓
report
```

 This is called drill-through or drill-down.

---

 ## 12.4 What Does "Cited Cube Rows" Mean?

 It means your result should identify the actual rows used.

 For example, don't just show:

```
Volume = +94,827
```

 Show the relevant source records:

```
row ID
company
period
account
practice
grade
quantity
price
amount
vintage
```

 The exact columns depend on your cube schema.

 The important thing is that the evaluator can say:

 > "These are the exact source rows that produced this number."

---

 ## 12.5 What Does "Vintage" Mean Here?

 Your data has a vintage / version.

 For example:

```
Vintage = 2
```

 You need to show that with the source rows.

 So the evaluator knows which version of the data was used.

 For example:

```
Company | Period | Account | Quantity | Amount | Vintage
RTAE1   | 2026-04| 41000   | 1,200    | ...    | 2
```

 This matters because the same business data might have multiple versions.

---

 ## 12.6 What You Need to Implement

 You basically need two things.

 ### 1\. Normal Result

```
Volume     +94,827
```

 ### 2\. Drill-Through Action

 When the user clicks:

```
Volume +94,827
```

 your backend runs another query against the cube to retrieve the source rows contributing to that Volume calculation.

 Then display those rows to the user.

 Conceptually:

```
User clicks
 ↓
Frontend sends:
company = RTAE1
period = Q2 2026
effect = volume
vintage = 2
 ↓
Backend queries cube
 ↓
Underlying rows
 ↓
Display rows to user
```

 The key idea 7.3 is asking:

 > "Don't just give me the calculated number. Give me a way to trace that number back to the actual database rows that produced it."

 So if the evaluator clicks Volume, they should be able to see the company, period, account, practice, grade, quantity, unit price, amount, vintage, etc. for the underlying rows.

 This is not another calculation requirement like 4.2/4.4.

 It's a traceability/audit requirement.

---

 # 13\. Approve / Reject / Lock

 ## 13.1 Requirement

 Also i am not sure for the below functionality also.

 ### 7.5 Approve / Reject / Lock

 The interface must let the appropriate human perform:

```
[Approve] [Reject] [Lock]
```

 But the UI must not bypass API permissions.

 The assignment specifically says the approve action should use the same permission rules as the API.

 So don't do:

```
Frontend
 ↓
directly modify Postgres
```

 Do:

```
Frontend
 ↓
API
 ↓
Permission checks
 ↓
Postgres / Temporal
```

---

 # 14\. Required vs What You Choose

 ## One Important Distinction: What is REQUIRED vs What YOU Choose

 | Area | Assignment requires | You choose |
| --- | --- | --- |
| Postgres | Governance guarantees | Exact table design |
| Plan states | Draft → In-Review → Approved → Locked → Superseded | Exact implementation |
| Self approval | Must fail | Exact error/UI |
| Covenant | Breach blocks approval | Exact covenant rules/thresholds |
| Driver trace | Required on every line | Exact JSON/schema |
| Scenarios | Driver overrides, no copied rows | Exact schema |
| Audit | Hash chain + tamper detection | Hash implementation |
| DSL | Real parser + AST | Parser implementation |
| SQL | Parameterized | Compiler implementation |
| Scope | Compiler-enforced | Exact RBAC implementation |
| Variance | Must tie | Exact decomposition implementation |
| Temporal | Durable workflow | Exact activity design |
| Second shock | Must have defined behaviour | You choose the behaviour |
| Approval timer | Required | You choose timeout/action |
| Agno team | Required | Number of members/mode |
| Team mode | Defend your choice | coordinate, route, etc. |
| UI | Five required things | UI framework/design |

---

 # 15\. Final Short Summary

 ## Don't Miss These

 Your evaluator should be able to do all of this.

---

 # 16\. Environment

```
docker compose up
      ↓
Postgres ✓
ClickHouse ✓
Temporal ✓
Seed data ✓
```

---

 # 17\. Plan Governance

```
Create plan
   ↓
Review
   ↓
Second person approves
   ↓
Lock
```

 And prove:

```
Self approval       → blocked
Covenant breach     → blocked
Locked edit         → blocked even via psql
Missing trace       → blocked
Scenario            → driver override, no copied rows
Audit tampering     → verifier fails
```

---

 # 18\. FinOpsExpr

```
English
 ↓
DSL
 ↓
AST
 ↓
Validation
 ↓
Parameterized SQL
 ↓
ClickHouse
```

 And prove:

```
Bad formula         → clear error
Unknown measure     → clear error
Bad aggregation     → clear error
Cycle               → clear error
Wrong scope         → rows restricted
Partition pruning   → EXPLAIN proves it
Grammar             → tests
```

---

 # 19\. Variance Bridge

```
Plan - Actual
     ↓
Price
Volume
Mix
FX
     ↓
Residual ≈ 0
```

 And prove:

```
ties at every level
volume + mix = quantity variance
practice mix ≠ suspicious zero
grade-within-practice mix ≠ suspicious zero
Poland Q2 legs materially non-zero
property tests pass
report + citations + vintage stored
agent cannot close report
```

---

 # 20\. Temporal

```
Driver shock
     ↓
Temporal
     ↓
Recompute affected lines
     ↓
Human approval wait
     ↓
Publish
```

 And prove:

```
Worker killed       → resumes
Same run twice      → no duplicate/change
Cancel              → works
Progress             → visible
Second shock         → defined behaviour
Approval wait        → survives restart
Reject               → nothing published
Timer                → defined timer/action
Commitment failure  → no half-applied state
Replay test         → passes
```

---

 # 21\. Agno

```
Question
 ↓
Team
 ↓
DSL
 ↓
Compiler
 ↓
Data
 ↓
Cited answer
```

 And prove:

```
No direct DB access
PII masked
Disclosure logged
Classification failure → block
Invented number → blocked
Driver proposal → human approval
Hostile data → treated as data
Scope widening → blocked
Team mode → justified with token/latency numbers
```

---

 # 22\. Browser

 The evaluator must see:

 1. DSL
2. Variance waterfall
3. Underlying cube rows + vintage
4. Live Temporal progress
5. Approve / Reject / Lock

 And the UI actions must go through the same authorization rules as the API.
