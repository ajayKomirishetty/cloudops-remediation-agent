"""Runtime configuration, read once from environment variables.

Everything that changes behaviour between local dev, CI, and a deployed
environment lives here so the rest of the code never touches os.environ.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Demo API keys. A real deployment would put SSO / OIDC in front of the API;
# keys are overridable via CLOUDOPS_API_KEYS="key:user:role,key:user:role".
DEFAULT_API_KEYS = {
    "viewer-demo-key": ("alice", "viewer"),
    "operator-demo-key": ("bob", "operator"),
}


def _parse_api_keys(raw: str | None) -> dict[str, tuple[str, str]]:
    if not raw:
        return dict(DEFAULT_API_KEYS)
    keys: dict[str, tuple[str, str]] = {}
    for entry in raw.split(","):
        key, user, role = entry.strip().split(":")
        keys[key] = (user, role)
    return keys


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "app" / "data" / "sources"
    db_path: Path = ROOT / "var" / "runs.sqlite3"
    # The fixture data is a snapshot; "now" is pinned so staleness checks are
    # deterministic. In production this would be datetime.now(timezone.utc).
    as_of: datetime = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
    stale_after_hours: float = 48.0
    idle_avg_cpu_pct: float = 5.0
    idle_max_cpu_pct: float = 20.0
    idle_network_mb: float = 200.0
    planner: str = "auto"  # auto | rules | claude
    claude_model: str = "claude-opus-5-5"
    max_agent_steps: int = 8
    tool_timeout_s: float = 2.0
    tool_max_attempts: int = 3
    tool_retry_backoff_s: float = 0.05
    api_keys: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DEFAULT_API_KEYS))
    allow_fault_injection: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        kwargs: dict = {}
        if v := env.get("CLOUDOPS_DATA_DIR"):
            kwargs["data_dir"] = Path(v)
        if v := env.get("CLOUDOPS_DB_PATH"):
            kwargs["db_path"] = Path(v)
        if v := env.get("CLOUDOPS_AS_OF"):
            kwargs["as_of"] = datetime.fromisoformat(v)
        if v := env.get("CLOUDOPS_PLANNER"):
            kwargs["planner"] = v
        if v := env.get("CLOUDOPS_CLAUDE_MODEL"):
            kwargs["claude_model"] = v
        if v := env.get("CLOUDOPS_TOOL_TIMEOUT_S"):
            kwargs["tool_timeout_s"] = float(v)
        if v := env.get("CLOUDOPS_ALLOW_FAULT_INJECTION"):
            kwargs["allow_fault_injection"] = v.lower() in {"1", "true", "yes"}
        kwargs["api_keys"] = _parse_api_keys(env.get("CLOUDOPS_API_KEYS"))
        return cls(**kwargs)
