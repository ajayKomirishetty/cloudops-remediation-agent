"""Deterministic safety checks that sit between the planner and the real world.

The planner (rules or LLM) is treated as untrusted: whatever it proposes is
re-checked here against policy and against what the run actually observed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.state import RunState
    from app.data.repository import CloudRepository

ALLOWED_ACTIONS = frozenset({"stop"})
PROTECTED_ENVIRONMENTS = frozenset({"prod"})
REDACTED = "[REDACTED: suspected prompt injection]"

_DESTRUCTIVE = re.compile(r"\b(terminate|delete|destroy|wipe|drop|purge)\b", re.I)
_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) instructions|disregard .{0,30}instructions"
    r"|^\s*system\s*:|you are now|act as (an? )?(admin|root))",
    re.I,
)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""


def check_goal(goal: str) -> Verdict:
    if _INJECTION.search(goal):
        return Verdict(False, "goal contains instruction-override text")
    if m := _DESTRUCTIVE.search(goal):
        return Verdict(False, f"destructive operation '{m.group(1).lower()}' is not permitted; only stopping idle instances is supported")
    return Verdict(True)


def sanitize_text(text: str) -> tuple[str, bool]:
    if _INJECTION.search(text):
        return REDACTED, True
    return text, False


def observed_utilization(state: "RunState") -> dict[str, dict[str, Any]]:
    """Latest successful get_utilization output per instance in this run."""
    seen: dict[str, dict[str, Any]] = {}
    for step in state.tool_outputs("get_utilization"):
        if step.error is None and isinstance(step.output, dict):
            seen[step.output["instance_id"]] = step.output
    return seen


def check_action(action: dict[str, Any], state: "RunState", repo: "CloudRepository") -> Verdict:
    iid = action.get("instance_id", "")
    if action.get("action") not in ALLOWED_ACTIONS:
        return Verdict(False, f"action '{action.get('action')}' is not allowed (only {sorted(ALLOWED_ACTIONS)})")
    try:
        inst = repo.get_instance(iid)
    except KeyError:
        return Verdict(False, "instance does not exist")
    if inst.environment in PROTECTED_ENVIRONMENTS:
        return Verdict(False, f"{inst.environment} instances are protected and cannot be changed by the agent")
    if inst.state != "running":
        return Verdict(False, f"instance is already {inst.state}")
    util = observed_utilization(state).get(iid)
    if util is None:
        return Verdict(False, "not grounded: utilization was never successfully observed in this run")
    if util.get("stale"):
        return Verdict(False, "not grounded: utilization data is stale")
    if util.get("assessment") != "idle":
        return Verdict(False, f"not grounded: observed assessment is '{util.get('assessment')}', not idle")
    return Verdict(True)


def check_executable(instance_id: str, repo: "CloudRepository") -> Verdict:
    """Last check right before execution, after a human approved."""
    try:
        inst = repo.get_instance(instance_id)
    except KeyError:
        return Verdict(False, "instance does not exist")
    if inst.environment in PROTECTED_ENVIRONMENTS:
        return Verdict(False, f"{inst.environment} instances are protected")
    return Verdict(True)
