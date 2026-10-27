"""Shared fixtures. Every test gets its own SQLite file and the deterministic planner."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent.state import RunState
from app.agent.tools import ToolContext, ToolRegistry
from app.config import Settings
from app.data.repository import CloudRepository
from app.faults import FaultState


@pytest.fixture
def settings(tmp_path) -> Settings:
    # Short timeouts keep the timeout/retry paths fast without changing behaviour.
    return replace(Settings(), db_path=tmp_path / "runs.sqlite3", planner="rules",
                   tool_timeout_s=0.2, tool_retry_backoff_s=0.0)


@pytest.fixture
def repo(settings) -> CloudRepository:
    return CloudRepository.from_dir(settings.data_dir)


@pytest.fixture
def registry(repo, settings) -> ToolRegistry:
    return ToolRegistry(ToolContext(repo, settings, FaultState()))


def observe(state: RunState, registry: ToolRegistry, tool: str, args: dict) -> None:
    """Run a tool and record it on the state the way the orchestrator does."""
    res = registry.execute(tool, args)
    state.add_step(type="tool_call", tool=tool, input=args, output=res.output, error=res.error)
