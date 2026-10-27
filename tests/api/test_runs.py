"""End-to-end API behaviour of runs with the deterministic rules planner."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings

OPERATOR = {"X-API-Key": "operator-demo-key"}
VIEWER = {"X-API-Key": "viewer-demo-key"}
GOAL = "Find idle dev and staging VMs and propose stopping them to save cost."
IDLE = ["i-dev-001", "i-dev-003", "i-stg-002"]


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = replace(Settings(), db_path=tmp_path / "runs.sqlite3", planner="rules")
    return TestClient(create_app(settings))


def start(client: TestClient, faults: list[str] | None = None) -> dict:
    res = client.post("/runs", headers=OPERATOR, json={"goal": GOAL, "faults": faults or []})
    assert res.status_code == 201, res.text
    return res.json()


def proposed(run: dict) -> list[str]:
    return sorted(p["instance_id"] for p in run["proposal"])


def test_happy_path_proposes_idle_non_prod_instances(client):
    run = start(client)
    assert run["status"] == "awaiting_approval"
    assert proposed(run) == IDLE
    assert not any(i.startswith("i-prd") for i in proposed(run))
    assert any(f["kind"] == "prompt_injection" for f in run["findings"])
    # Fixture data deliberately has missing/stale metrics for these three.
    assert "3 of 9 instance(s) could not be assessed (i-dev-005, i-dev-006, i-stg-003)" in run["summary"]


def test_approve_subset_then_second_decision_conflicts(client):
    run = start(client)
    res = client.post(f"/runs/{run['run_id']}/approval", headers=OPERATOR,
                      json={"decision": "approve", "instance_ids": ["i-dev-001"]})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "completed"
    assert body["results"] == {"i-dev-001": "stopped"}
    again = client.post(f"/runs/{run['run_id']}/approval", headers=OPERATOR, json={"decision": "approve"})
    assert again.status_code == 409


def test_reject(client):
    run = start(client)
    res = client.post(f"/runs/{run['run_id']}/approval", headers=OPERATOR, json={"decision": "reject"})
    assert res.json()["status"] == "rejected"
    assert res.json()["results"] == {}


def test_auth_and_roles(client):
    assert client.get("/runs").status_code == 401
    assert client.get("/runs", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/runs", headers=VIEWER).status_code == 200
    assert client.post("/runs", headers=VIEWER, json={"goal": GOAL}).status_code == 403
    run = start(client)
    assert client.post(f"/runs/{run['run_id']}/approval", headers=VIEWER,
                       json={"decision": "approve"}).status_code == 403


@pytest.mark.parametrize("body", [
    {"goal": "  "}, {"goal": GOAL, "extra": 1}, {"goal": GOAL, "faults": ["bogus"]},
])
def test_invalid_create_requests(client, body):
    assert client.post("/runs", headers=OPERATOR, json=body).status_code == 422


def test_unknown_run_is_404(client):
    assert client.get("/runs/missing", headers=OPERATOR).status_code == 404


def test_metrics_unavailable_is_reported_as_inconclusive_not_completed(client):
    run = start(client, ["metrics_unavailable"])
    assert run["status"] == "failed"
    assert run["proposal"] == []
    assert "none of the 9 instance(s) could be assessed" in run["summary"]
    assert "No instances qualified" not in run["summary"]
    assert "could not be retrieved" in run["proposal_summary"]


def test_stale_data_is_reported_as_inconclusive(client):
    run = start(client, ["stale_data"])
    assert run["status"] == "failed"
    assert run["proposal"] == []
    assert any(f["kind"] == "data_quality" and "stale" in f["message"] for f in run["findings"])


@pytest.mark.parametrize("fault, skipped", [
    ("metrics_timeout", "i-dev-003"),
    ("invalid_tool_response", "i-stg-002"),
])
def test_single_instance_metric_fault_skips_only_that_instance(client, fault, skipped):
    run = start(client, [fault])
    assert run["status"] == "awaiting_approval"
    assert proposed(run) == [i for i in IDLE if i != skipped]
    assert skipped in run["summary"] and "could not be assessed" in run["summary"]


def test_cloud_api_error_once_is_retried_on_execution(client):
    run = start(client, ["cloud_api_error_once"])
    res = client.post(f"/runs/{run['run_id']}/approval", headers=OPERATOR, json={"decision": "approve"})
    body = res.json()
    assert body["status"] == "completed"
    assert set(body["results"].values()) == {"stopped"}
    first_stop = next(s for s in body["steps"] if s["type"] == "execute")
    assert first_stop["attempts"] == 2


def test_fault_injection_can_be_disabled(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "r.sqlite3", planner="rules", allow_fault_injection=False)
    c = TestClient(create_app(settings))
    assert c.post("/runs", headers=OPERATOR, json={"goal": GOAL, "faults": ["stale_data"]}).status_code == 403
    assert c.get("/meta").json()["faults"] == []
