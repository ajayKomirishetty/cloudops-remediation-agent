from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.planner import RulesPlanner, environments_for_goal
from app.agent.state import RunState
from app.api.schemas import ApprovalRequest, CreateRunRequest
from app.config import DEFAULT_API_KEYS, Settings, _parse_api_keys
from app.faults import FaultState


@pytest.mark.parametrize("goal, envs", [
    ("idle dev VMs", ["dev"]),
    ("unused staging instances", ["staging"]),
    ("idle dev and staging VMs", ["dev", "staging"]),
    ("non-prod waste", ["dev", "staging"]),
    ("non production compute", ["dev", "staging"]),
    ("idle production servers", ["prod"]),
    ("idle instances", ["dev", "staging"]),  # default never includes prod
    ("developer laptops", ["dev", "staging"]),  # word boundary: 'developer' is not 'dev'
])
def test_environments_for_goal(goal, envs):
    assert environments_for_goal(goal) == envs


def test_rules_planner_starts_by_listing_each_environment():
    step = RulesPlanner().next_step(RunState(goal="idle dev and staging VMs", created_by="t"))
    assert [(c.name, c.args) for c in step.tool_calls] == [
        ("list_instances", {"environment": "dev"}), ("list_instances", {"environment": "staging"}),
    ]


def test_rules_planner_stops_if_listing_failed():
    state = RunState(goal="idle dev VMs", created_by="t")
    state.add_step(type="tool_call", tool="list_instances", input={"environment": "dev"}, error="boom")
    step = RulesPlanner().next_step(state)
    assert not step.tool_calls and "Could not list" in step.text


def test_parse_api_keys():
    assert _parse_api_keys(None) == DEFAULT_API_KEYS
    assert _parse_api_keys("k1:ann:operator, k2:joe:viewer") == {"k1": ("ann", "operator"), "k2": ("joe", "viewer")}
    with pytest.raises(ValueError):
        _parse_api_keys("malformed")


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDOPS_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("CLOUDOPS_PLANNER", "rules")
    monkeypatch.setenv("CLOUDOPS_TOOL_TIMEOUT_S", "2.5")
    monkeypatch.setenv("CLOUDOPS_ALLOW_FAULT_INJECTION", "false")
    monkeypatch.setenv("CLOUDOPS_LOG_LEVEL", "debug")
    s = Settings.from_env()
    assert (s.db_path, s.planner, s.tool_timeout_s, s.allow_fault_injection, s.log_level) == (
        tmp_path / "x.db", "rules", 2.5, False, "DEBUG")


def test_fault_state():
    with pytest.raises(ValueError, match="unknown faults"):
        FaultState(["bogus"])
    f = FaultState(["cloud_api_error_once"])
    assert f.fire_once("cloud_api_error_once") is True
    assert f.fire_once("cloud_api_error_once") is False
    assert FaultState().fire_once("cloud_api_error_once") is False


def test_create_run_request_normalises():
    req = CreateRunRequest(goal="  find idle VMs  ", faults=["stale_data", "stale_data"])
    assert req.goal == "find idle VMs" and req.faults == ["stale_data"]


@pytest.mark.parametrize("body", [
    {"goal": "x" * 501}, {"goal": "   ab   "}, {"goal": "ok goal", "faults": ["nope"]},
])
def test_create_run_request_rejects(body):
    with pytest.raises(ValidationError):
        CreateRunRequest(**body)


@pytest.mark.parametrize("body", [
    {"decision": "maybe"}, {"decision": "approve", "instance_ids": []},
    {"decision": "approve", "comment": "c" * 501}, {"decision": "reject", "extra": 1},
])
def test_approval_request_rejects(body):
    with pytest.raises(ValidationError):
        ApprovalRequest(**body)
