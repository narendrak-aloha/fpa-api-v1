"""Stored reports must never bypass the scope used for new queries."""
import pytest
from fastapi.testclient import TestClient

import app as api
from fpa_project import bridge_service
from fpa_project.governance import Principal

REPORT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def client(monkeypatch):
    principal = Principal("analyst", "Analyst", frozenset({"analyst"}), frozenset({"RTPL1"}), True)
    api.app.dependency_overrides[api.current_user] = lambda: principal
    monkeypatch.setattr(bridge_service, "report_in_scope", lambda report, companies: False)
    def forbidden(*args, **kwargs):
        pytest.fail("report contents must not be read or changed before authorization")
    for name in ("load_report", "citations_for", "set_status"):
        monkeypatch.setattr(bridge_service, name, forbidden)
    with TestClient(api.app) as client:
        yield client
    api.app.dependency_overrides.clear()


@pytest.mark.parametrize("suffix,method", [("", "GET"), ("/citations", "GET"), ("/status", "POST")])
def test_report_routes_refuse_out_of_scope_before_access(client, suffix, method):
    response = client.request(method, f"/api/v1/variance-reports/{REPORT}{suffix}",
                              **({"json": {"status": "CLOSED"}} if method == "POST" else {}))
    assert response.status_code == 404


def test_authorized_report_read_uses_authenticated_scope(client, monkeypatch):
    def allowed(report, companies):
        assert report == REPORT
        assert companies == frozenset({"RTPL1"})
        return True
    monkeypatch.setattr(bridge_service, "report_in_scope", allowed)
    monkeypatch.setattr(bridge_service, "load_report", lambda report: {"report_id": report})
    assert client.get(f"/api/v1/variance-reports/{REPORT}").json() == {"report_id": REPORT}


def test_empty_scope_never_reads_database(monkeypatch):
    monkeypatch.setattr(bridge_service, "postgres", lambda: pytest.fail("empty scope must fail closed"))
    assert not bridge_service.report_in_scope(REPORT, frozenset())
