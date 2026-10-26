"""Fault injection for testing failure handling end to end.

Faults are requested per run (API field `faults`) and are only honoured when
Settings.allow_fault_injection is true, which a production deploy turns off.
Bad source data and the prompt-injection tag are always present in the
fixtures, so they need no switch.
"""

from __future__ import annotations

from typing import Literal

FaultName = Literal[
    "metrics_unavailable",   # metrics dependency down: every call fails (retries exhausted)
    "metrics_timeout",       # metrics for i-dev-003 hang past the tool timeout
    "invalid_tool_response", # metrics for i-stg-002 come back malformed
    "stale_data",            # clock moves 7 days forward, all metrics become stale
    "cloud_api_error_once",  # first stop_instance call fails transiently, retry succeeds
]

ALL_FAULTS: tuple[str, ...] = FaultName.__args__  # type: ignore[attr-defined]

TIMEOUT_INSTANCE = "i-dev-003"
MALFORMED_INSTANCE = "i-stg-002"
STALE_SHIFT_DAYS = 7


class FaultState:
    """Active faults for one run, plus counters for 'fail once' behaviour."""

    def __init__(self, faults: list[str] | None = None) -> None:
        unknown = set(faults or []) - set(ALL_FAULTS)
        if unknown:
            raise ValueError(f"unknown faults: {sorted(unknown)}")
        self.active = frozenset(faults or [])
        self._fired: set[str] = set()

    def __contains__(self, name: str) -> bool:
        return name in self.active

    def fire_once(self, name: str) -> bool:
        """True the first time an active fault is checked, False afterwards."""
        if name in self.active and name not in self._fired:
            self._fired.add(name)
            return True
        return False
