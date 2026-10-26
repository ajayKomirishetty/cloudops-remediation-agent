"""Run state and its persistence.

A run is persisted after every step, so the full execution history survives
a restart and a run paused for approval can be resumed by another process.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["running", "awaiting_approval", "completed", "rejected", "failed", "blocked"]
StepType = Literal["plan", "tool_call", "guardrail", "approval", "execute"]
TERMINAL: frozenset[str] = frozenset({"completed", "rejected", "failed", "blocked"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StepRecord(BaseModel):
    index: int
    type: StepType
    tool: str | None = None
    call_id: str | None = None
    input: dict[str, Any] | None = None
    output: Any = None
    error: str | None = None
    attempts: int | None = None
    duration_ms: float | None = None
    note: str | None = None
    ts: datetime = Field(default_factory=utcnow)


class ProposedAction(BaseModel):
    instance_id: str
    action: Literal["stop"] = "stop"
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    monthly_savings_usd: float = 0.0


class ApprovalDecision(BaseModel):
    decided_by: str
    decision: Literal["approve", "reject"]
    instance_ids: list[str] = Field(default_factory=list)
    comment: str = ""
    ts: datetime = Field(default_factory=utcnow)


class Finding(BaseModel):
    kind: str  # e.g. data_quality, guardrail, tool_failure, prompt_injection
    message: str
    instance_id: str | None = None


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str
    status: RunStatus = "running"
    created_by: str
    planner: str = "rules"
    faults: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    steps: list[StepRecord] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    proposal: list[ProposedAction] = Field(default_factory=list)
    proposal_summary: str = ""
    approval: ApprovalDecision | None = None
    results: dict[str, str] = Field(default_factory=dict)  # instance_id -> stopped/already_stopped/failed: ...
    summary: str = ""
    error: str | None = None

    def add_step(self, **kwargs: Any) -> StepRecord:
        step = StepRecord(index=len(self.steps), **kwargs)
        self.steps.append(step)
        return step

    def add_finding(self, kind: str, message: str, instance_id: str | None = None) -> None:
        if not any(f.kind == kind and f.message == message for f in self.findings):
            self.findings.append(Finding(kind=kind, message=message, instance_id=instance_id))

    def tool_outputs(self, tool: str) -> list[StepRecord]:
        return [s for s in self.steps if s.type == "tool_call" and s.tool == tool]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


class RunNotFoundError(KeyError):
    pass


class RunStore:
    """SQLite-backed store; each run is one JSON document plus indexed columns."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " created_by TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL)"
        )
        self._conn.commit()

    def save(self, state: RunState) -> None:
        state.updated_at = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, status, created_by, created_at, doc) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, doc=excluded.doc",
                (state.run_id, state.status, state.created_by, state.created_at.isoformat(), state.model_dump_json()),
            )
            self._conn.commit()

    def get(self, run_id: str) -> RunState:
        with self._lock:
            row = self._conn.execute("SELECT doc FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return RunState.model_validate(json.loads(row[0]))

    def list(self, limit: int = 50) -> list[RunState]:
        with self._lock:
            rows = self._conn.execute("SELECT doc FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [RunState.model_validate(json.loads(r[0])) for r in rows]
