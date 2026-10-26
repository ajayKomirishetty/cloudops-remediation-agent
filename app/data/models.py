"""Validated domain records for the two data sources (inventory + metrics)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Environment = Literal["dev", "staging", "prod"]
InstanceState = Literal["running", "stopped"]


class Instance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(pattern=r"^i-[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=128)
    environment: Environment
    owner: str = Field(min_length=1)
    region: str = Field(min_length=1)
    instance_type: str = Field(min_length=1)
    hourly_cost_usd: float = Field(ge=0)
    state: InstanceState
    tags: dict[str, str] = Field(default_factory=dict)


class MetricRecord(BaseModel):
    """7-day utilization rollup for one instance."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(pattern=r"^i-[a-z0-9-]+$")
    collected_at: datetime
    avg_cpu_pct: float = Field(ge=0, le=100)
    max_cpu_pct: float = Field(ge=0, le=100)
    network_mb: float = Field(ge=0)

    @field_validator("collected_at")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("collected_at must include a timezone")
        return v

    @model_validator(mode="after")
    def _avg_not_above_max(self) -> "MetricRecord":
        if self.avg_cpu_pct > self.max_cpu_pct:
            raise ValueError("avg_cpu_pct cannot exceed max_cpu_pct")
        return self


class DataIssue(BaseModel):
    source: Literal["inventory", "metrics"]
    location: str  # record index or CSV line number
    severity: Literal["error", "warning"]
    message: str


class DataQualityReport(BaseModel):
    inventory_total: int = 0
    inventory_valid: int = 0
    metrics_total: int = 0
    metrics_valid: int = 0
    issues: list[DataIssue] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")
