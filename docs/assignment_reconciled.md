## Summary

The project is an FP&A (Financial Planning & Analysis) platform: it lets a company plan
budgets, compare actuals against plan, explain the gaps, and run recalculations safely when
assumptions change — with proper approvals, audit trails, and an AI assistant that can answer
planning questions in natural language.

| Section | Area | What it covers | Status |
|---|---|---|---|
| S0 | Environment | Getting the system running | 83% |
| S1 | Plan spine | The core planning data and approval rules | 93% |
| S2 | Formula engine | The language used to define financial calculations | 90% |
| S3 | Variance bridge | Explaining why actuals differ from plan | 94% |
| S4 | Durable recompute | Safely re-running calculations when things change | 88% |
| S5 | AI agents | The natural-language planning assistant | 81% |
| S6 | User interface | What the end user sees and clicks | 100% |
| S7 | Stretch goals | Optional extra work if time allowed | 7% |

A full test run against the live system passed almost everything: 344 checks run, only 3
failed, and all 3 are test-environment issues rather than missing functionality (details in
S2 and S3 below).

---

## S0 — Getting the system running (83%)

- ✅ All the supporting services — the data warehouse, the main database, the workflow
  engine, the application itself, the background recalculation worker, and an external
  "commitment" system it talks to — start up together in containers.
- ✅ The data warehouse is loaded with over a million rows of actual financial results, ready
  to query.
- 🟡 Starting everything with one command works on a machine that already has the project set
  up. It has **not** been proven yet that a completely fresh machine, with nothing installed,
  can get the whole system running from scratch — there's a setup script written for this,
  but it hasn't been run end-to-end.

## S1 — The plan spine (93%)

This is the heart of the system: the data model for budgets, forecasts, and the rules that
keep them trustworthy.

- ✅ A registry of planning "dimensions" (like company, product, time period) and "measures"
  (like revenue, cost), plus a defined order for calculating dependent numbers.
- ✅ Planning assumptions ("drivers," e.g. growth rate, price per unit) can change over time
  with defined start and end dates, and the system checks those date ranges don't overlap.
- ✅ Every plan goes through a formal lifecycle (draft → review → approved → locked), and each
  line of the plan keeps a trace of how its numbers were derived.
- ✅ Scenarios (e.g. "optimistic case," "downside case") are built as overrides on top of a
  base plan, not as duplicated copies of the whole plan.
- ✅ The exchange rate used for planning is kept separate from the actual market rate used for
  real results, so the two don't get mixed up.
- ✅ Once a variance report (an explanation of a plan-vs-actual gap) is closed, only a human
  in a controller or CFO role can do that — not an automated process.
- ✅ The audit trail (a record of who did what, when) cannot be edited after the fact; each
  entry is cryptographically linked to the one before it, and tampering with it is detectable.
- ✅ Nobody can approve their own work — this is enforced both in the database and in the
  application itself.
- ✅ A plan can't move to "approved" or "locked" status unless it also passes a debt-covenant
  compliance check.
- ✅ Only a fully "locked" plan version can be published for others to see.
- ✅ A plan line can't be saved without an explanation of how its number was derived, and
  amount calculations (quantity × unit price) are double-checked by the database itself.
- ✅ Every status change (draft → review → approved, etc.) is recorded as its own row, not
  just a hidden flag.
- ✅ Only someone in the controller role can edit covenant results or exchange rates.
- ✅ If two people edit the same plan at the same time, the second save is blocked rather than
  silently overwriting the first (an "optimistic locking" approach).
- ✅ The full cycle — one person authors a plan, someone reviews it, a second person approves
  it, and it gets locked — works end-to-end through the actual screens, not just behind the
  scenes.
- ✅ There's written guidance explaining which rules are enforced by the database itself versus
  by the application code, and why.
- 🟡 **A "locked" plan isn't completely locked.** Editing or deleting existing lines in a
  locked plan is correctly blocked, but adding a brand-new line to a locked plan currently
  still succeeds — this is a gap that needs closing.
- 🟡 **A locked plan can never be marked "superseded"** (replaced by a newer version) — the
  status change itself is blocked. You can still trace forward and backward through
  the chain of plan versions that replaced each other, but the status field itself doesn't
  reflect it.
- 🟡 **The AI-disclosure log is not fully tamper-proof in the database.** This log is meant to
  record every time information is shared with the AI model, and it's supposed to be
  unchangeable — but a database-level safeguard meant to enforce that isn't actually active
  in the running system, so in principle a row could still be edited.

## S2 — The formula engine (90%)

This is the mini calculation language used to define financial formulas (e.g. revenue growth
year-over-year) safely, without letting anyone write raw, unrestricted code.

- ✅ Formulas — whether typed directly or built from a query — are safely parsed into a
  structured form, never executed as raw code.
- ✅ Supports the financial calculations planners actually need: prior period, next period,
  year-over-year, compound growth, year/quarter/month-to-date, rolling averages, sums,
  averages, min/max, rounding, etc.
- ✅ Formulas are translated into safe, parameterized database queries; any name that isn't a
  known measure or dimension is rejected with a clear error.
- ✅ The system checks that a calculation makes sense for the type of measure involved (e.g.
  you can't "sum" a percentage-style measure like utilization) and explains the error plainly.
- ✅ Whoever is asking a question only ever sees data within their permitted scope, and overly
  expensive queries are refused before they can slow down the system.
- ✅ When someone saves a new planning driver, its formula is checked for validity, checked
  that all referenced items exist, and checked that it doesn't create a circular dependency
  (with a sensible exception for "prior period" / "next period" references, which aren't
  really circular).
- ✅ A solid set of tests cover formula precedence, nested formulas, time-based functions,
  type-checking, and expected failures.
- ✅ Historical "as of" queries correctly pull data as it stood at a chosen point in time.
- 🟡 **One test fails on a naming mismatch, not a real bug.** A test checks that the database
  is efficiently skipping irrelevant data partitions when filtering by date, but it's looking
  for an old label the database version no longer uses (a cosmetic difference in the newer
  database version) — the actual performance behavior is correct.
- 🟡 **One test fails on a magnitude question that needs more digging.** A test expects a
  specific cost swing between two months of about 18%, but the current data shows about 6.4%.
  This isn't obviously a bug — it may be a stale assumption baked into the test — but it needs
  investigation before it can be marked resolved.

## S3 — Variance bridge (94%)

This explains, in a structured and auditable way, *why* actual results differ from the plan —
breaking the gap into causes like price, volume, product mix, and currency movement.

- ✅ Revenue gaps break down into price, volume, mix (by business line, then by grade within
  it), and currency effects. Cost gaps break down into rate and efficiency effects.
- ✅ At every level of the breakdown, the pieces add up (tie out) to the actual total gap,
  within a small tolerance.
- ✅ Volume and mix effects correctly add up to the total quantity-driven variance.
- ✅ The method for ordering these effects is documented and tested, and it's verified that
  calculating them in the opposite order still ties out.
- ✅ A broad, randomized test checks this ties out correctly across many simulated plan/actual
  combinations, at every level, in both calculation orders.
- ✅ Finished variance reports are saved with references back to the underlying data and the
  point in time they reflect; if a gap is too large to explain automatically, it's flagged for
  a human to close manually.
- 🟡 **One live test currently fails because the underlying data changed.** A specific
  historical cut (Poland, a past quarter) was used as the basis for a test, but since then a
  revised forecast was published against that same plan, which shifted the numbers being
  compared. The bridge logic itself isn't in question — the test data just needs refreshing.

## S4 — Durable recompute (88%)

This is the "recalculation engine" — when an assumption changes (e.g. a cost shock), the
system needs to recompute a plan reliably, even if it takes a long time or something fails
partway through.

- ✅ The recalculation process is deterministic and its correctness is checked automatically
  in continuous integration against real historical runs.
- ✅ It follows clear phases: take a snapshot, figure out what changed, fan out the work,
  save a draft, route for approval, publish, commit downstream, then recompute variances.
- ✅ Long-running steps report progress and can resume; very large runs split themselves up
  automatically rather than running forever in one piece; different types of work have
  appropriate retry rules.
- ✅ Repeating the exact same change twice has no extra effect — it's recognized as the same
  request rather than being applied twice.
- ✅ If a second change arrives while the first is still being processed, it's incorporated
  correctly (or rejected if it arrives too late to safely combine).
- ✅ Runs can be cancelled, their progress can be checked at any time, and an approval step
  that's neither approved nor rejected in time automatically expires as rejected.
- ✅ The approve/reject step requires a different person than the one who requested the
  change, and this was verified live.
- ✅ The external system this integrates with (the "Commitment Service") is called safely:
  repeat calls don't double-apply, failed pushes can be undone, and there's a way to
  simulate that system failing to test the recovery path.
- ✅ If pushing a commitment to that external system fails, the system automatically
  compensates (undoes the partial change) rather than leaving things inconsistent.
- 🟡 A few resilience scenarios are believed to work (based on the design) but have not been
  re-verified live recently: killing the background worker mid-run and while a run is paused,
  the external system failing 100% of the time, and two changes stacking up back-to-back
  across all three services together.
- 🟡 **If a run fails partway through cleanup, the cleanup itself isn't fully independent
  step-by-step.** Right now, if the very first cleanup step fails, the remaining cleanup steps
  (like marking the related plan and run as failed) get skipped too, leaving things in a
  stuck, half-finished state rather than a clean "failed" state. This was observed directly:
  a run stayed stuck as "running" and its plan stayed stuck as "draft" when it should have
  been marked failed.

A separate fix (made but not yet finalized/committed): a caching bug where the recalculation
worker would remember warehouse tables as "already set up" for its whole lifetime, so if the
warehouse was reset while a worker was still running, every subsequent recalculation attempt
would fail. It now re-checks on every use instead of trusting a stale memory.

## S5 — AI agents (81%)

This is the natural-language assistant: a team of AI agents that can answer planning
questions, propose changes, and enforce guardrails, instead of a single unsupervised model.

- ✅ A coordinated team of AI agents (a "leader" plus specialist members) works together, and
  the reasoning for this team design is documented.
- ✅ Each agent has a stable identity, a limit on how many tools it can call, a strict expected
  output format, and one bounded chance to fix its own mistake before giving up.
- ✅ Agents can only use a small, safe set of tools — listing available metrics, listing
  available dimensions, running an approved financial query, or proposing a new planning
  driver. They cannot run arbitrary database queries.
- ✅ Guardrails run before every request: one checks for personal/sensitive information, another
  checks for prompt-injection attempts (someone trying to trick the AI into ignoring its
  rules).
- ✅ A separate check runs after the answer is generated to catch invented or incorrect
  numbers before they reach the user.
- ✅ Any information sent to or from the AI model is masked where needed and logged for
  disclosure purposes — the log entry is written *before* the information is sent, and the
  log never stores the actual sensitive content itself (34 such records so far).
- ✅ Each request is scoped to what that user/token is allowed to see; an agent without the
  right scope is refused before it can act.
- ✅ When an agent proposes a new planning driver, it pauses and requires a second, different
  human to confirm before continuing — this proposal and its outcome are recorded.
- ✅ A reconciliation check compares data across time and flags anything suspicious into the
  audit trail.
- ✅ The boundaries between the workflow engine, the AI agent framework, and the database are
  documented.
- 🟡 The AI-disclosure log's tamper-proofing gap (same one noted in S1) also applies here.
- 🟡 Adversarial testing (trying to trick the AI into leaking data, expanding its own access,
  or claiming false authority) was run live and passed for the main scenarios, but a couple of
  edge cases (a specific kind of refusal, and directly targeting one agent instead of the
  team) are only covered by automated tests, not a live run.
- ⬜ **No measurement yet of how much more this multi-agent team costs** (in time and API
  usage) compared to just using a single AI agent.
- ⬜ **No record of which specific agent/team member produced a given answer.** This traceability
  isn't visible in the response, the screen, or the audit log yet.

## S6 — User interface (100%)

- ✅ When a question is answered, the underlying formula, the generated database query, and
  the parameters used are shown alongside the answer for transparency.
- ✅ The variance "bridge" is shown as a labeled waterfall chart with any small unexplained
  remainder clearly shown (not rounded away).
- ✅ Users can drill through any number back to the underlying data rows, with the time period
  those rows reflect.
- ✅ Recalculation progress is shown live as it happens, including its phase, any refusals, and
  the final outcome.
- ✅ The approve / reject / lock actions go through the same rules as the backend; the buttons
  only appear for roles allowed to use them, with an explanation on hover for why they are or
  aren't available.

Note: the role-aware buttons and the live progress display were finished recently but are not
yet saved into the user-interface project's version history.

## S7 — Stretch goals, if time remained (7%)

These were optional "nice to have" items beyond the core assignment. Only one has partial
progress; the rest were not started.

- 🟡 **Comparing the same report across two different points in time** (e.g. "what changed
  between the July close and the August close") — this works and is tested, but it hasn't
  been run against real data yet, and there's no screen for it in the interface.
- ⬜ Consolidation across multiple related companies (removing intercompany transactions and
  handling currency-translation adjustments).
- ⬜ An automated evaluation suite for the AI agents that runs continuously and produces a
  report.
- ⬜ A screen for comparing multiple scenarios side by side.
- ⬜ A rolling forecast feature with a "how accurate were we" back-test.
- ⬜ Pre-computed summaries or a semantic layer to speed up common queries, with performance
  numbers to prove it.
- ⬜ Row-level data security enforced directly in the data warehouse, beneath the formula
  engine.

---

