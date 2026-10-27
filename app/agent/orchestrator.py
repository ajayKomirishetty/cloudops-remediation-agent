"""The agent loop: planner -> tools -> guardrails -> human approval -> execution.

The orchestrator owns all side effects. Planners only suggest tool calls;
the orchestrator executes them through the registry, records every step on
the run state, persists the state after each step, and is the only place
that can call stop_instance - and only for actions a human approved.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.agent.guardrails import check_action, check_executable, check_goal, observed_utilization
from app.agent.planner import Planner, PlannerError, make_planner
from app.agent.state import ApprovalDecision, ProposedAction, RunState, RunStore
from app.agent.tools import TERMINAL_TOOL, ToolContext, ToolRegistry
from app.config import Settings
from app.data.repository import CloudRepository
from app.faults import FaultState
from app.observability import current_run_id, log_event

PlannerFactory = Callable[[Settings, ToolRegistry], Planner]


class InvalidRunStateError(RuntimeError):
    """The requested transition is not valid for the run's current status."""


class Orchestrator:
    def __init__(self, settings: Settings, repo: CloudRepository, store: RunStore,
                 planner_factory: PlannerFactory = make_planner) -> None:
        self.settings = settings
        self.repo = repo
        self.store = store
        self.planner_factory = planner_factory
        # Serialises approvals so two concurrent decisions cannot both execute the same run.
        self._decide_lock = threading.Lock()

    def _registry(self, faults: list[str]) -> ToolRegistry:
        return ToolRegistry(ToolContext(self.repo, self.settings, FaultState(faults)))

    # ----------------------------------------------------------------- planning phase

    def start(self, goal: str, user: str, faults: list[str] | None = None) -> RunState:
        faults = list(faults or [])
        registry = self._registry(faults)  # raises ValueError on unknown faults before anything is stored
        state = RunState(goal=goal, created_by=user, faults=faults)
        token = current_run_id.set(state.run_id)
        try:
            log_event("run.started", user=user, faults=faults)
            verdict = check_goal(goal)
            if not verdict.allowed:
                state.add_step(type="guardrail", note=f"goal blocked: {verdict.reason}")
                state.add_finding("guardrail", f"Goal blocked: {verdict.reason}")
                state.status = "blocked"
                state.summary = f"Request blocked by policy: {verdict.reason}."
                self.store.save(state)
                log_event("run.blocked", logging.WARNING, reason=verdict.reason)
                return state

            try:
                planner = self.planner_factory(self.settings, registry)
            except Exception as exc:  # noqa: BLE001 - e.g. missing SDK or credentials
                return self._fail(state, f"planner could not be created: {exc}")
            state.planner = planner.name
            self.store.save(state)
            self._loop(state, planner, registry)
            return state
        finally:
            current_run_id.reset(token)

    def _loop(self, state: RunState, planner: Planner, registry: ToolRegistry) -> None:
        for turn in range(self.settings.max_agent_steps):
            try:
                step = planner.next_step(state)
            except PlannerError as exc:
                self._fail(state, f"planner error: {exc}")
                return
            if step.text:
                state.add_step(type="plan", note=step.text)
            if not step.tool_calls:
                # The planner stopped without submitting a plan.
                self._fail(state, step.text or "planner finished without submitting a plan")
                return

            results = []
            submitted = None
            for call in step.tool_calls:
                res = registry.execute(call.name, call.args)
                state.add_step(type="tool_call", tool=call.name, call_id=call.id, input=call.args,
                               output=res.output, error=res.error, attempts=res.attempts,
                               duration_ms=res.duration_ms)
                log_event("tool.call", logging.INFO if res.ok else logging.WARNING, tool=call.name,
                          ok=res.ok, error_kind=res.error_kind, attempts=res.attempts, duration_ms=res.duration_ms)
                self._record_findings(state, call.name, call.args, res)
                results.append((call, res))
                if call.name == TERMINAL_TOOL and res.ok:
                    submitted = call.args
            self.store.save(state)

            if submitted is not None:
                self._finalise_proposal(state, submitted)
                return
            planner.observe(results)

        self._fail(state, f"agent did not submit a plan within {self.settings.max_agent_steps} steps")

    def _record_findings(self, state: RunState, tool: str, args: dict, res) -> None:
        if not res.ok:
            iid = args.get("instance_id") if isinstance(args, dict) else None
            target = f" for {iid}" if iid else ""
            state.add_finding("tool_failure", f"{tool}{target} failed ({res.error_kind}) after "
                              f"{res.attempts} attempt(s): {res.error}", iid)
            return
        if tool == "list_instances":
            for w in res.output.get("warnings", []):
                state.add_finding("prompt_injection", w, w.split(" on ")[-1].split(" ")[0])
        elif tool == "get_utilization" and res.output["assessment"] == "insufficient_data":
            why = "data is stale" if res.output.get("collected_at") else "no valid metrics"
            state.add_finding("data_quality", f"{res.output['instance_id']}: {why}; not eligible",
                              res.output["instance_id"])

    def _finalise_proposal(self, state: RunState, plan: dict) -> None:
        observed = observed_utilization(state)
        savings: dict[str, float] = {}
        for s in state.tool_outputs("estimate_savings"):
            if s.error is None:
                savings.update(s.output["per_instance_monthly_usd"])

        seen: set[str] = set()
        for action in plan.get("actions", []):
            iid = action.get("instance_id", "")
            if iid in seen:
                continue
            seen.add(iid)
            verdict = check_action(action, state, self.repo)
            if not verdict.allowed:
                state.add_step(type="guardrail", input=action, note=f"rejected {iid}: {verdict.reason}")
                state.add_finding("guardrail", f"Proposed action on {iid} rejected: {verdict.reason}", iid)
                log_event("guardrail.rejected", logging.WARNING, instance_id=iid, reason=verdict.reason)
                continue
            u = observed[iid]
            hourly = self.repo.get_instance(iid).hourly_cost_usd
            state.proposal.append(ProposedAction(
                instance_id=iid,
                reason=action.get("reason", ""),
                evidence={k: u.get(k) for k in ("avg_cpu_pct", "max_cpu_pct", "network_mb", "collected_at", "age_hours")},
                monthly_savings_usd=savings.get(iid, round(hourly * 730, 2)),
            ))

        state.proposal_summary = plan.get("summary", "")
        total = round(sum(p.monthly_savings_usd for p in state.proposal), 2)
        # Instances the agent tried to assess but could not: every metrics call failed,
        # or the metrics it got were stale/invalid. "Nothing idle" is only a valid
        # conclusion for instances that were actually assessed.
        checked = {s.input["instance_id"] for s in state.tool_outputs("get_utilization")
                   if isinstance(s.input, dict) and "instance_id" in s.input}
        unassessed = sorted(checked - observed.keys()) + sorted(
            k for k, v in observed.items() if v.get("assessment") == "insufficient_data")
        gap = (f" {len(unassessed)} of {len(checked)} instance(s) could not be assessed "
               f"({', '.join(unassessed)}); see findings.") if unassessed else ""
        if state.proposal:
            state.status = "awaiting_approval"
            state.summary = (f"{len(state.proposal)} instance(s) proposed for stop, saving ~${total:,.2f}/month. "
                             "Waiting for operator approval." + gap)
        elif checked and len(unassessed) == len(checked):
            state.status = "failed"
            state.error = f"no instance could be assessed: utilization unavailable or unusable for {', '.join(unassessed)}"
            state.summary = (f"Run inconclusive: none of the {len(checked)} instance(s) could be assessed, "
                             "so idle instances may have been missed. Nothing to approve.")
        else:
            state.status = "completed"
            state.summary = "No instances qualified for remediation; nothing to approve." + gap
        state.add_step(type="plan", note=f"proposal ready: {len(state.proposal)} action(s), ${total}/month")
        self.store.save(state)
        log_event("run.proposal", actions=len(state.proposal), monthly_savings_usd=total, status=state.status)

    def _fail(self, state: RunState, error: str) -> RunState:
        state.status = "failed"
        state.error = error
        state.summary = f"Run failed: {error}"
        self.store.save(state)
        log_event("run.failed", logging.ERROR, error=error)
        return state

    # ----------------------------------------------------------------- approval phase

    def decide(self, run_id: str, user: str, decision: str, instance_ids: list[str] | None = None,
               comment: str = "") -> RunState:
        token = current_run_id.set(run_id)
        try:
            with self._decide_lock:
                return self._decide(self.store.get(run_id), user, decision, instance_ids, comment)
        finally:
            current_run_id.reset(token)

    def _decide(self, state: RunState, user: str, decision: str, instance_ids: list[str] | None,
                comment: str) -> RunState:
        if state.status != "awaiting_approval":
            raise InvalidRunStateError(f"run is '{state.status}', not awaiting approval")
        proposed = [p.instance_id for p in state.proposal]
        chosen = list(dict.fromkeys(instance_ids or proposed))
        unknown = sorted(set(chosen) - set(proposed))
        if unknown:
            raise ValueError(f"instance(s) not in the proposal: {unknown}")

        state.approval = ApprovalDecision(decided_by=user, decision=decision,
                                          instance_ids=chosen if decision == "approve" else [],
                                          comment=comment)
        state.add_step(type="approval", note=f"{decision} by {user}" + (f": {comment}" if comment else ""),
                       input={"instance_ids": chosen if decision == "approve" else []})
        log_event("run.decision", user=user, decision=decision, instance_ids=chosen)

        if decision == "reject":
            state.status = "rejected"
            state.summary = f"Proposal rejected by {user}; no changes made."
            self.store.save(state)
            return state

        self.store.save(state)
        self._execute(state, chosen)
        return state

    def _execute(self, state: RunState, instance_ids: list[str]) -> None:
        registry = self._registry(state.faults)
        for iid in instance_ids:
            verdict = check_executable(iid, self.repo)
            if not verdict.allowed:
                state.results[iid] = f"blocked: {verdict.reason}"
                state.add_step(type="guardrail", note=f"execution of {iid} blocked: {verdict.reason}")
                state.add_finding("guardrail", f"Execution on {iid} blocked: {verdict.reason}", iid)
                continue
            t0 = time.perf_counter()
            res = registry.execute("stop_instance", {"instance_id": iid}, allow_hidden=True)
            state.add_step(type="execute", tool="stop_instance", input={"instance_id": iid},
                           output=res.output, error=res.error, attempts=res.attempts,
                           duration_ms=res.duration_ms or round((time.perf_counter() - t0) * 1000, 2))
            log_event("tool.call", logging.INFO if res.ok else logging.ERROR, tool="stop_instance",
                      instance_id=iid, ok=res.ok, attempts=res.attempts)
            if res.ok:
                state.results[iid] = res.output["result"]
            else:
                state.results[iid] = f"failed: {res.error}"
                state.add_finding("tool_failure", f"stop_instance for {iid} failed: {res.error}", iid)
            self.store.save(state)

        failed = [k for k, v in state.results.items() if not v.startswith(("stopped", "already_stopped"))]
        done = len(state.results) - len(failed)
        if failed and not done:
            state.status = "failed"
            state.error = f"no instance could be stopped: {', '.join(failed)}"
            state.summary = f"Approved by {state.approval.decided_by}, but execution failed for every instance."
        else:
            state.status = "completed"
            state.summary = f"Approved by {state.approval.decided_by}; stopped {done} instance(s)" + (
                f", {len(failed)} failed ({', '.join(failed)})." if failed else ".")
        self.store.save(state)
        log_event("run.completed", status=state.status, results=state.results)
