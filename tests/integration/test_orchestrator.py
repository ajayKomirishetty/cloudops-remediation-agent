"""Orchestrator + tools + guardrails + store wired together, driven by a scripted planner.

The scripted planner stands in for a misbehaving LLM: it can propose anything,
so these tests check that the orchestrator, not the planner, enforces safety.
"""

from __future__ import annotations

import threading

import pytest

from app.agent.orchestrator import InvalidRunStateError, Orchestrator
from app.agent.planner import PlannerError, PlannerStep, ToolCall
from app.agent.state import RunStore

GOAL = "Find idle dev and staging VMs to save cost"


class ScriptedPlanner:
    name = "scripted"

    def __init__(self, steps: list) -> None:
        self.steps = list(steps)
        self.observed: list = []

    def next_step(self, state) -> PlannerStep:
        if not self.steps:
            return PlannerStep(text="out of script")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def observe(self, results) -> None:
        self.observed.extend(results)


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c-{name}", name=name, args=args)


def utils(*ids: str) -> PlannerStep:
    return PlannerStep([call("get_utilization", instance_id=i) for i in ids])


def submit(*ids: str, action: str = "stop") -> PlannerStep:
    return PlannerStep([call("submit_remediation_plan", summary="scripted",
                             actions=[{"instance_id": i, "action": action, "reason": "idle"} for i in ids])])


@pytest.fixture
def store(settings):
    return RunStore(settings.db_path)


def orch(settings, repo, store, *steps) -> Orchestrator:
    return Orchestrator(settings, repo, store, planner_factory=lambda *_: ScriptedPlanner(list(steps)))


def guardrail_msgs(state) -> list[str]:
    return [f.message for f in state.findings if f.kind == "guardrail"]


def test_scripted_happy_path_persists_full_trace(settings, repo, store):
    state = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001")).start(GOAL, "bob")
    assert state.status == "awaiting_approval"
    assert [p.instance_id for p in state.proposal] == ["i-dev-001"]
    assert state.proposal[0].evidence["avg_cpu_pct"] == 1.2
    loaded = store.get(state.run_id)
    assert [s.type for s in loaded.steps] == [s.type for s in state.steps]


@pytest.mark.parametrize("iid, observe_first, reason", [
    ("i-prd-001", True, "protected"),           # idle prod instance
    ("i-dev-003", False, "never successfully observed"),  # idle, but agent never looked
    ("i-dev-002", True, "not idle"),
    ("i-stg-004", True, "not idle"),            # the instance whose tag tried to inject
    ("i-fake-1", False, "does not exist"),      # hallucinated id
])
def test_unsafe_or_ungrounded_proposals_are_dropped(settings, repo, store, iid, observe_first, reason):
    steps = ([utils(iid)] if observe_first else []) + [submit(iid)]
    state = orch(settings, repo, store, *steps).start(GOAL, "bob")
    assert state.proposal == []
    assert state.status in {"completed", "failed"}
    assert any(iid in m and reason in m for m in guardrail_msgs(state)), guardrail_msgs(state)


def test_non_stop_action_is_rejected(settings, repo, store):
    # The plan schema accepts any action string; the guardrail is what enforces stop-only.
    state = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001", action="terminate")).start(GOAL, "bob")
    assert state.proposal == [] and state.status == "completed"
    assert any("i-dev-001" in m and "not allowed" in m for m in guardrail_msgs(state))


def test_duplicate_actions_are_collapsed(settings, repo, store):
    state = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001", "i-dev-001")).start(GOAL, "bob")
    assert [p.instance_id for p in state.proposal] == ["i-dev-001"]


def test_mixed_proposal_keeps_only_safe_actions(settings, repo, store):
    state = orch(settings, repo, store, utils("i-dev-001", "i-prd-001"),
                 submit("i-dev-001", "i-prd-001")).start(GOAL, "bob")
    assert [p.instance_id for p in state.proposal] == ["i-dev-001"]
    assert any("i-prd-001" in m for m in guardrail_msgs(state))


def test_planner_that_never_submits_fails_after_step_budget(settings, repo, store):
    steps = [utils("i-dev-001")] * (settings.max_agent_steps + 2)
    state = orch(settings, repo, store, *steps).start(GOAL, "bob")
    assert state.status == "failed" and "within 8 steps" in state.error
    assert sum(1 for s in state.steps if s.type == "tool_call") == settings.max_agent_steps


@pytest.mark.parametrize("step, error", [
    (PlannerError("model refused"), "planner error: model refused"),
    (PlannerStep(text="I am done."), "I am done."),
])
def test_planner_errors_fail_the_run(settings, repo, store, step, error):
    state = orch(settings, repo, store, step).start(GOAL, "bob")
    assert state.status == "failed" and state.error == error


def test_planner_factory_failure_fails_the_run(settings, repo, store):
    def boom(*_):
        raise RuntimeError("no credentials")
    state = Orchestrator(settings, repo, store, planner_factory=boom).start(GOAL, "bob")
    assert state.status == "failed" and "no credentials" in state.error


def test_planner_cannot_call_hidden_or_unknown_tools(settings, repo, store):
    p = ScriptedPlanner([PlannerStep([call("stop_instance", instance_id="i-dev-001"), call("rm_rf")]),
                         utils("i-dev-001"), submit("i-dev-001")])
    state = Orchestrator(settings, repo, store, planner_factory=lambda *_: p).start(GOAL, "bob")
    assert repo.get_instance("i-dev-001").state == "running"
    assert all(not r.ok and "unknown tool" in r.error for _, r in p.observed[:2])
    assert state.status == "awaiting_approval"


def test_tool_errors_are_fed_back_to_the_planner(settings, repo, store):
    p = ScriptedPlanner([utils("i-zzz-999"), submit()])
    Orchestrator(settings, repo, store, planner_factory=lambda *_: p).start(GOAL, "bob")
    (_, res), = p.observed
    assert res.error_kind == "not_found"


def test_approval_survives_restart(settings, repo, store):
    run = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001")).start(GOAL, "bob")
    fresh = Orchestrator(settings, repo, RunStore(settings.db_path))  # new process, same DB
    done = fresh.decide(run.run_id, "carol", "approve")
    assert done.status == "completed" and done.results == {"i-dev-001": "stopped"}
    assert repo.get_instance("i-dev-001").state == "stopped"


def test_already_stopped_instance_is_idempotent(settings, repo, store):
    run = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001")).start(GOAL, "bob")
    repo.stop_instance("i-dev-001")  # someone stopped it out of band
    done = Orchestrator(settings, repo, store).decide(run.run_id, "bob", "approve")
    assert done.status == "completed" and done.results == {"i-dev-001": "already_stopped"}


def test_approving_instance_outside_proposal_is_rejected(settings, repo, store):
    run = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001")).start(GOAL, "bob")
    with pytest.raises(ValueError, match="not in the proposal"):
        Orchestrator(settings, repo, store).decide(run.run_id, "bob", "approve", ["i-prd-001"])
    assert store.get(run.run_id).status == "awaiting_approval"


def test_concurrent_approvals_execute_once(settings, repo, store):
    run = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001")).start(GOAL, "bob")
    o = Orchestrator(settings, repo, store)
    outcomes: list[str] = []

    def approve():
        try:
            o.decide(run.run_id, "bob", "approve")
            outcomes.append("ok")
        except InvalidRunStateError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=approve) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["conflict"] * 4 + ["ok"]
    assert sum(1 for s in store.get(run.run_id).steps if s.type == "execute") == 1


def test_cloud_error_every_time_fails_the_run(settings, repo, store, monkeypatch):
    from app.agent import tools
    from app.agent.tools import TransientToolError

    run = orch(settings, repo, store, utils("i-dev-001"), submit("i-dev-001")).start(GOAL, "bob")

    def always_429(*_):
        raise TransientToolError("429")
    monkeypatch.setitem(tools.TOOLS, "stop_instance", tools.TOOLS["stop_instance"].__class__(
        **{**tools.TOOLS["stop_instance"].__dict__, "fn": always_429}))
    done = Orchestrator(settings, repo, store).decide(run.run_id, "bob", "approve")
    assert done.status == "failed" and done.results["i-dev-001"].startswith("failed")
    assert repo.get_instance("i-dev-001").state == "running"
