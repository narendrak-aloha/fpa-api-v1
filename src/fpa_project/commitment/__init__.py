"""The downstream Commitment Service finance uses to reserve budget.

A separate process with its own Postgres schema, reached only over HTTP. It is
part of the deliverable, but it is not part of the FP&A system: the point of
writing it is that the workflow has to be correct against a counterparty that
is not, and a shared transaction would hide exactly that problem.
"""
