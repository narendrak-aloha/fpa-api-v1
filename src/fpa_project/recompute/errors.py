"""The retryable / non-retryable split, in one place.

Temporal decides whether to retry from the error type, so the distinction has
to be made where the error is raised rather than guessed at the call site. The
rule: a *business* answer that will be the same on every attempt is permanent,
and anything that is a statement about the network, the disk or the hour is
worth another go.
"""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

# Named in every RetryPolicy so the type survives serialization into history.
PERMANENT = "PermanentRecomputeError"
TRANSIENT = "TransientRecomputeError"
# A human decision the governance store would not accept: the requester
# approving their own plan, someone without the role, an unknown user. Not
# retried, and not fatal either -- the run stays parked for a valid decision,
# because one bad signal must not be able to destroy a re-forecast.
REFUSED = "DecisionRefusedError"


class PermanentRecomputeError(Exception):
    """A verdict that will not change on a second attempt.

    An unknown plan version, a version that is not LOCKED, a shock naming a
    driver nobody has heard of, a self-approval. Retrying these only delays
    the moment somebody has to look at them.
    """


class TransientRecomputeError(Exception):
    """A failure that says nothing about whether the request was right.

    A dropped connection, a ClickHouse timeout, an HTTP 503. Worth retrying.
    """


def permanent(message: str) -> ApplicationError:
    return ApplicationError(message, type=PERMANENT, non_retryable=True)


def refused(message: str) -> ApplicationError:
    return ApplicationError(message, type=REFUSED, non_retryable=True)


def transient(message: str) -> ApplicationError:
    return ApplicationError(message, type=TRANSIENT)


def classify_http(status: int, body: str) -> ApplicationError:
    """Map a Commitment Service response onto the split.

    4xx is the service telling us the request is wrong, which a retry cannot
    fix; 408 and 429 are the two that are about timing rather than content.
    Everything else, including every 5xx, is worth another attempt.
    """
    if 400 <= status < 500 and status not in (408, 429):
        return permanent(f"commitment service rejected the request: {status} {body[:200]}")
    return transient(f"commitment service failed: {status} {body[:200]}")
