# Re-Forecast Temporal Workflow Requirements

## 1. Start a Re-Forecast Workflow

A driver shock/request starts the re-forecast workflow.

### Example

A user changes Poland utilization:

```text
Poland utilization: 75% → 70%
```

This change starts a Temporal workflow to recompute the affected plan data.

---

## 2. Run the Recomputation

The workflow must:

1. Find which plan rows are affected by the driver change.
2. Recalculate the affected rows.
3. Save the calculated rows as draft lines.

The recalculated data must **not be published immediately**.

---

## 3. Be Durable

The workflow must survive Temporal worker failures.

If the Temporal worker crashes:

- The workflow state must not be lost.
- The workflow must continue when the worker comes back.
- It must not restart the entire workflow from zero.
- Previously completed Activities should not be unnecessarily executed again.

Temporal workflow history is used to restore the workflow state.

---

## 4. Wait for Human Approval

After the affected rows are calculated, the workflow must wait for human approval.

The workflow should:

1. Calculate the new draft lines.
2. Save the draft lines.
3. Enter an approval-waiting state.
4. Wait for a human decision.

The human approval is sent to the Temporal workflow using a **Temporal Signal**.

```text
Application
    ↓
Human approves
    ↓
Temporal Signal
    ↓
Running Workflow
```

The approval wait must survive a Temporal worker restart.

---

## 5. Bound the Approval Waiting Time

The workflow must not wait for human approval forever.

A Temporal timer should run alongside the approval wait.

The workflow should handle two possible outcomes:

```text
              ┌── Human approves
Approval wait ┤
              └── Timer expires
```

The assignment does not prescribe the exact timeout duration.

The implementation should choose a reasonable duration and document the reasoning behind it.

---

## 6. Handle Rejection

If the human rejects the re-forecast:

- Do not publish the changes to ClickHouse.
- Do not call the Commitment Service.
- Mark the draft/recomputation as rejected or cancelled.
- Finish the workflow cleanly.

Example:

```text
Draft Calculation
       ↓
Human Rejects
       ↓
Mark Re-Forecast as Rejected
       ↓
Workflow Completed
```

---

## 7. Publish Only After Approval and Locking

The workflow must ensure that only a **Locked Plan Version** can be published.

The workflow must not simply trust that the application API already performed this validation.

The Temporal workflow/Activity must independently verify the required state before publishing.

```text
Human Approval
      ↓
Verify Plan Version = LOCKED
      ↓
Publish to ClickHouse
```

If the plan version is not locked, publishing must not happen.

---

## 8. Call the Commitment Service

After successfully publishing the changes to ClickHouse:

1. Identify the affected commitments.
2. Send the affected commitments to the Commitment Service.
3. Use a stable idempotency key for the request.

Example:

```text
ClickHouse Publication
        ↓
Affected Commitments
        ↓
Commitment Service
```

The Commitment Service call must be retryable.

If the service temporarily fails:

```text
Commitment Service
       ↓
Temporary Failure
       ↓
Temporal Retry
       ↓
Commitment Service
```

Temporal should retry the Activity according to its retry policy.

---

## 9. Handle Permanent Commitment Service Failure

If the Commitment Service continues failing after the configured retries, the workflow must execute real compensation.

The system must not leave:

```text
ClickHouse State
       ≠
Commitment Ledger State
```

The compensation strategy must restore consistency between ClickHouse and the Commitment ledger.

The compensation logic must be explicitly implemented rather than simply logging the error and finishing the workflow.

---

## 10. Make Activities Retryable and Idempotent

External operations must happen inside Temporal Activities.

Activities include operations such as:

- PostgreSQL queries/updates
- ClickHouse queries/updates
- HTTP API calls
- Commitment Service calls

The Workflow itself must not directly perform these operations.

### Activity Requirements

Every Activity should be safe to execute more than once.

For example:

```text
Activity attempt #1
       ↓
Worker crashes
       ↓
Activity attempt #2
       ↓
Same operation remains safe
```

Each Activity should have its own retry configuration.

Retry behavior should distinguish between:

### Retryable Errors

Examples:

- Temporary network failure
- Database connection failure
- HTTP 503
- Timeout

These errors should normally be retried.

### Non-Retryable Errors

Examples:

- Invalid request
- Invalid plan version
- Validation failure
- Business rule violation

These should normally fail immediately without unnecessary retries.

---

## 11. Handle Cancellation

A user must be able to cancel a running re-forecast.

Temporal must stop the workflow cleanly.

Cancellation must not leave the system with a half-published revision.

For example:

```text
Re-Forecast Running
       ↓
User Cancels
       ↓
Stop Remaining Processing
       ↓
Clean Up / Mark Cancelled
       ↓
Workflow Completed
```

The implementation must ensure that cancellation does not result in inconsistent ClickHouse or Commitment data.

---

## 12. Show Progress

The application UI should be able to retrieve the current state of a running workflow.

The UI should be able to display:

- Current phase
- Number of dirty rows
- Number of processed rows

Example:

```text
Phase:       RECOMPUTING
Dirty Rows:  10,000
Processed:   6,500
```

This workflow state can be exposed through Temporal's **Query** mechanism or another appropriate read-only workflow state mechanism.

---

## 13. Handle a Second Driver Shock

A second driver change may arrive while a re-forecast is already running.

### Example

First shock:

```text
Poland utilization
75% → 70%
```

A re-forecast starts.

While it is running, another shock arrives:

```text
Poland utilization
70% → 65%
```

The system must explicitly define what happens.

The assignment expects a **Temporal Update Handler** with validation.

The implementation must choose one of the following approaches:

### Option A — Reject the Second Shock

The Update Handler validates the request and rejects it if another re-forecast is already running.

```text
Running Re-Forecast
       ↑
Second Shock
       ↓
Update Handler
       ↓
Reject
```

### Option B — Incorporate the Second Shock

The Update Handler validates the new shock and incorporates it into the currently running workflow.

The important requirement is:

> A second shock must not be silently ignored.

---

## 14. Handle Large Recomputation

A large set of dirty rows should not necessarily be processed by one workflow execution.

The recomputation should be split into **Child Workflows**.

Example:

```text
Parent Re-Forecast Workflow
          ↓
   ┌──────┼──────┐
   ↓      ↓      ↓
Child 1 Child 2 Child 3
   ↓      ↓      ↓
 Rows    Rows    Rows
```

Each Child Workflow can process a portion of the dirty rows.

This provides better scalability and keeps the parent workflow manageable.

---

## 15. Use Continue-As-New for Large Workflow History

Temporal workflows accumulate workflow history.

If the workflow history becomes too large, use **Continue-As-New**.

Continue-As-New starts a new execution with the required current state while keeping the same logical workflow.

Conceptually:

```text
Workflow Run #1
      ↓
History becomes large
      ↓
Continue-As-New
      ↓
Workflow Run #2
      ↓
Continue processing
```

This prevents workflow history from growing indefinitely.

---

## 16. Keep Workflow Code Deterministic

Temporal Workflow code must be deterministic because Temporal can replay the workflow history.

The Workflow must **not directly** perform operations such as:

- PostgreSQL queries
- ClickHouse queries
- HTTP API calls
- Normal system clock access
- Random number generation

These operations belong in Activities.

### Incorrect

```text
Workflow
   ↓
Query PostgreSQL
   ↓
Call ClickHouse
   ↓
Call HTTP API
```

### Correct

```text
Workflow
   ↓
Activity → PostgreSQL
   ↓
Activity → ClickHouse
   ↓
Activity → HTTP API
```

For time-based operations, use Temporal's workflow-safe time/timer mechanisms rather than the normal system clock.

For random or unique values, use deterministic workflow-safe mechanisms or generate them inside an Activity when appropriate.

---

## 17. Pass the Replay Test

The workflow must be replayable.

A recorded Temporal workflow history should be replayed against the current workflow code.

Example:

```text
Recorded Workflow History
          ↓
Current Workflow Code
          ↓
Temporal Replay
          ↓
Deterministic Result
```

If replay succeeds, it demonstrates that the Workflow code is deterministic.

The implementation should include a replay test using a recorded Temporal workflow history.

---

# Implementation Checklist

The Temporal implementation should cover the following:

- [ ] Start workflow from a driver shock.
- [ ] Identify affected/dirty plan rows.
- [ ] Recalculate affected rows.
- [ ] Save recalculated rows as drafts.
- [ ] Survive Temporal worker crashes.
- [ ] Wait for human approval using a Temporal Signal.
- [ ] Add a timeout for the approval wait.
- [ ] Handle approval.
- [ ] Handle rejection.
- [ ] Verify the plan version is `LOCKED` before publishing.
- [ ] Publish approved changes to ClickHouse.
- [ ] Call the Commitment Service after ClickHouse publication.
- [ ] Use a stable idempotency key.
- [ ] Configure Activity retries.
- [ ] Distinguish retryable and non-retryable errors.
- [ ] Implement compensation for permanent Commitment Service failure.
- [ ] Support workflow cancellation.
- [ ] Expose workflow progress.
- [ ] Implement a Temporal Update Handler for a second driver shock.
- [ ] Explicitly reject or incorporate a second shock.
- [ ] Split large recomputations into Child Workflows.
- [ ] Use Continue-As-New when workflow history becomes too large.
- [ ] Keep Workflow code deterministic.
- [ ] Keep database/HTTP operations inside Activities.
- [ ] Implement Temporal workflow replay testing.
