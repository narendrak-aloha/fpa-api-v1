"""The Commitment Service's contract, against its real Postgres.

The assignment fixes this contract, so these tests are written against the
contract rather than against the implementation: the same key twice creates
nothing new, no key applies every time, DELETE compensates, and the failure
rate can be turned up while the service is running.

Needs the stack (``make docker-local-run-d``). Skipped, not failed, when
Postgres is not reachable -- a unit suite that silently needs a database is
worse than one that says so.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from db.config import database_url

pytestmark = pytest.mark.integration


def postgres_reachable() -> bool:
    try:
        create_engine(database_url(), connect_args={"connect_timeout": 2}).connect().close()
        return True
    except Exception:
        return False


pytest.importorskip("fastapi.testclient")
if not postgres_reachable():
    pytest.skip("needs Postgres: run `make docker-local-run-d`", allow_module_level=True)

from fpa_project.commitment import service  # noqa: E402


@pytest.fixture
def client():
    with TestClient(service.app) as test_client:
        test_client.post("/admin/failure-rate", json={"rate": 0.0})
        yield test_client
        test_client.post("/admin/failure-rate", json={"rate": 0.0})


@pytest.fixture
def body():
    return {
        "plan_version": "PV-TEST", "revision": 99, "scenario": "base",
        "category": "Revenue", "amount": "1234.56",
    }


def test_the_same_key_twice_creates_nothing_new(client, body):
    key = f"test-{uuid.uuid4()}"
    first = client.post("/commitments", json=body, headers={"Idempotency-Key": key})
    second = client.post("/commitments", json=body, headers={"Idempotency-Key": key})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["commitment_id"] == first.json()["commitment_id"]
    assert second.json()["replayed"] is True

    listing = client.get("/commitments", params={"key_prefix": key}).json()["commitments"]
    assert len(listing) == 1


def test_a_different_amount_under_the_same_key_still_replays(client, body):
    """The key is the identity, not the payload.

    This is the behaviour that makes a retry safe: an activity that retries
    after a timeout may send a body built from a fresh read, and the service
    must still not create a second reservation.
    """
    key = f"test-{uuid.uuid4()}"
    first = client.post("/commitments", json=body, headers={"Idempotency-Key": key})
    second = client.post("/commitments", json={**body, "amount": "9999.99"},
                         headers={"Idempotency-Key": key})
    assert second.json()["commitment_id"] == first.json()["commitment_id"]


def test_without_a_key_it_applies_every_time(client, body):
    """The path that exists so it is visible whether the workflow uses it.

    This one does not: commit_to_treasury always sends a key derived from the
    plan version and revision.
    """
    plan_version = f"PV-NOKEY-{uuid.uuid4().hex[:8]}"
    payload = {**body, "plan_version": plan_version}
    first = client.post("/commitments", json=payload)
    second = client.post("/commitments", json=payload)

    assert first.json()["commitment_id"] != second.json()["commitment_id"]
    listing = client.get("/commitments", params={"plan_version": plan_version}).json()["commitments"]
    assert len(listing) == 2


def test_delete_releases_and_is_safe_to_repeat(client, body):
    key = f"test-{uuid.uuid4()}"
    created = client.post("/commitments", json=body, headers={"Idempotency-Key": key}).json()

    assert client.delete(f"/commitments/{created['commitment_id']}").status_code == 200
    # Compensation gets retried, so releasing twice has to converge rather than
    # error the second time.
    assert client.delete(f"/commitments/{created['commitment_id']}").status_code == 200

    listing = client.get("/commitments", params={"key_prefix": key}).json()["commitments"]
    assert listing[0]["state"] == "RELEASED"


def test_deleting_an_unknown_commitment_is_a_404(client):
    assert client.delete("/commitments/cmt_does_not_exist").status_code == 404


def test_the_failure_rate_is_settable_at_runtime(client, body):
    """Turned up on a running service, because that is how it will be used."""
    client.post("/admin/failure-rate", json={"rate": 1.0, "mode": "error"})
    assert client.get("/admin/failure-rate").json()["rate"] == 1.0

    response = client.post("/commitments", json=body, headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
    assert response.status_code == 500

    client.post("/admin/failure-rate", json={"rate": 0.0})
    assert client.post(
        "/commitments", json=body, headers={"Idempotency-Key": f"k-{uuid.uuid4()}"}
    ).status_code == 201


def test_a_total_failure_creates_nothing(client, body):
    """At 100% in error mode the ledger must be untouched, not half-written."""
    plan_version = f"PV-FAIL-{uuid.uuid4().hex[:8]}"
    client.post("/admin/failure-rate", json={"rate": 1.0, "mode": "error"})
    for _ in range(5):
        client.post(
            "/commitments",
            json={**body, "plan_version": plan_version},
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
        )
    client.post("/admin/failure-rate", json={"rate": 0.0})

    listing = client.get("/commitments", params={"plan_version": plan_version}).json()["commitments"]
    assert listing == []


def test_listing_is_not_subject_to_the_failure_injection(client, body):
    """A counterparty that cannot be inspected cannot be reconciled.

    Compensation sweeps by key prefix, so if the listing failed alongside the
    writes there would be no way back to a consistent state.
    """
    key = f"test-{uuid.uuid4()}"
    client.post("/commitments", json=body, headers={"Idempotency-Key": key})
    client.post("/admin/failure-rate", json={"rate": 1.0, "mode": "error"})
    assert client.get("/commitments", params={"key_prefix": key}).status_code == 200


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with create_engine(database_url()).begin() as conn:
        conn.execute(
            text(f"DELETE FROM {service.SCHEMA}.commitment WHERE plan_version LIKE 'PV-%'")
        )
