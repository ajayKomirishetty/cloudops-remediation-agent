"""Request/response contracts for the HTTP API (validated at the boundary)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.faults import ALL_FAULTS


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=3, max_length=500)
    faults: list[str] = Field(default_factory=list, max_length=len(ALL_FAULTS))

    @field_validator("goal")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("goal must contain at least 3 non-blank characters")
        return v

    @field_validator("faults")
    @classmethod
    def _known_faults(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(ALL_FAULTS))
        if unknown:
            raise ValueError(f"unknown fault(s) {unknown}; allowed: {sorted(ALL_FAULTS)}")
        return list(dict.fromkeys(v))


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    instance_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    comment: str = Field(default="", max_length=500)


class RunSummary(BaseModel):
    run_id: str
    goal: str
    status: str
    created_by: str
    created_at: str
    proposed: int
    summary: str


class Principal(BaseModel):
    user: str
    role: Literal["viewer", "operator"]
