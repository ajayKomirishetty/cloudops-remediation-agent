from __future__ import annotations

import time
from dataclasses import replace

import pytest
from pydantic import BaseModel

from app.agent.tools import (
    TOOLS,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolSpec,
    TransientToolError,
    assess,
    monthly_cost,
)
from app.faults import FaultState


class _In(BaseModel):
    x: int


class _Out(BaseModel):
    y: int


def _registry(ctx: ToolContext, fn) -> ToolRegistry:
    spec = ToolSpec("t", "test tool", _In, _Out, fn, {})
    return ToolRegistry(ctx, {"t": spec})


@pytest.fixture
def ctx(repo, settings):
    return ToolContext(repo, settings, FaultState())


def test_transient_error_is_retried_until_success(ctx):
    calls = []

    def flaky(_, args):
        calls.append(1)
        if len(calls) < 3:
            raise TransientToolError("503")
        return {"y": args.x}

    res = _registry(ctx, flaky).execute("t", {"x": 7})
    assert res.ok and res.output == {"y": 7} and res.attempts == 3


def test_retries_are_bounded(ctx):
    res = _registry(ctx, lambda *_: (_ for _ in ()).throw(TransientToolError("503"))).execute("t", {"x": 1})
    assert not res.ok and res.error_kind == "transient" and res.attempts == ctx.settings.tool_max_attempts


def test_timeout_is_reported_and_retried(ctx):
    def slow(*_):
        time.sleep(ctx.settings.tool_timeout_s * 2)
        return {"y": 1}

    res = _registry(ctx, slow).execute("t", {"x": 1})
    assert res.error_kind == "timeout" and res.attempts == ctx.settings.tool_max_attempts


@pytest.mark.parametrize("fn, kind", [
    (lambda *_: {"y": "not-an-int"}, "invalid_response"),
    (lambda *_: 1 / 0, "internal"),
    (lambda *_: (_ for _ in ()).throw(ToolError("not_found", "nope")), "not_found"),
])
def test_non_retryable_errors_fail_after_one_attempt(ctx, fn, kind):
    res = _registry(ctx, fn).execute("t", {"x": 1})
    assert not res.ok and res.error_kind == kind and res.attempts == 1


def test_invalid_arguments_never_reach_the_tool(ctx):
    called = []
    res = _registry(ctx, lambda *_: called.append(1) or {"y": 1}).execute("t", {"x": "abc"})
    assert res.error_kind == "invalid_input" and "invalid arguments" in res.error and not called


def test_hidden_tool_requires_allow_hidden(registry):
    assert registry.execute("stop_instance", {"instance_id": "i-dev-001"}).error == "unknown tool 'stop_instance'"
    assert "stop_instance" not in {t.name for t in registry.planner_tools()}
    assert registry.execute("stop_instance", {"instance_id": "i-dev-001"}, allow_hidden=True).ok


def test_unknown_tool(registry):
    res = registry.execute("rm_rf", {})
    assert res.error_kind == "invalid_input"


def test_extra_arguments_rejected(registry):
    assert registry.execute("get_utilization", {"instance_id": "i-dev-001", "force": True}).error_kind == "invalid_input"


@pytest.mark.parametrize("avg, peak, net, stale, expected", [
    (1.0, 5.0, 10.0, False, "idle"),
    (4.99, 19.99, 199.9, False, "idle"),
    (5.0, 5.0, 10.0, False, "active"),     # thresholds are strict
    (1.0, 20.0, 10.0, False, "active"),
    (1.0, 5.0, 200.0, False, "active"),
    (1.0, 5.0, 10.0, True, "insufficient_data"),
])
def test_assess_thresholds(settings, avg, peak, net, stale, expected):
    assert assess(avg, peak, net, stale, settings) == expected


def test_list_instances_redacts_injected_tags(registry):
    res = registry.execute("list_instances", {"environment": "staging"})
    assert res.ok
    sandbox = next(i for i in res.output["instances"] if i["instance_id"] == "i-stg-004")
    assert all("ignore previous instructions" not in str(v).lower() for v in sandbox["tags"].values())
    assert any("i-stg-004" in w for w in res.output["warnings"])


def test_get_utilization_uses_latest_record(registry):
    out = registry.execute("get_utilization", {"instance_id": "i-dev-001"}).output
    assert (out["avg_cpu_pct"], out["max_cpu_pct"], out["network_mb"]) == (1.2, 6.5, 20)
    assert out["assessment"] == "idle"


def test_get_utilization_missing_metrics_is_insufficient(registry):
    out = registry.execute("get_utilization", {"instance_id": "i-dev-006"}).output
    assert out["assessment"] == "insufficient_data"


def test_unknown_instance_is_not_found(registry):
    assert registry.execute("get_utilization", {"instance_id": "i-zzz-999"}).error_kind == "not_found"


def test_estimate_savings(registry, repo):
    out = registry.execute("estimate_savings", {"instance_ids": ["i-dev-001"]}).output
    assert out["total_monthly_usd"] == monthly_cost(repo.get_instance("i-dev-001").hourly_cost_usd)


@pytest.mark.parametrize("fault, iid, kind", [
    ("metrics_unavailable", "i-dev-001", "transient"),
    ("metrics_timeout", "i-dev-003", "timeout"),
    ("invalid_tool_response", "i-stg-002", "invalid_response"),
])
def test_faults_surface_as_typed_errors(repo, settings, fault, iid, kind):
    reg = ToolRegistry(ToolContext(repo, replace(settings, tool_max_attempts=1), FaultState([fault])))
    assert reg.execute("get_utilization", {"instance_id": iid}).error_kind == kind


def test_stale_data_fault_marks_metrics_stale(repo, settings):
    reg = ToolRegistry(ToolContext(repo, settings, FaultState(["stale_data"])))
    out = reg.execute("get_utilization", {"instance_id": "i-dev-001"}).output
    assert out["stale"] is True and out["assessment"] == "insufficient_data"


def test_all_tool_schemas_are_strict():
    for spec in TOOLS.values():
        assert spec.input_schema["additionalProperties"] is False
        assert set(spec.input_schema["required"]) == set(spec.input_schema["properties"])
