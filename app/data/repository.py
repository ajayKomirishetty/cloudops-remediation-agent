"""Data access: the only module that knows where instances and metrics live.

The repository wraps the validated fixtures and plays the role of the cloud
provider API (inventory, CloudWatch-style metrics, and the stop action).
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.data.loader import load_sources
from app.data.models import DataQualityReport, Instance, MetricRecord


class InstanceNotFoundError(KeyError):
    pass


class CloudRepository:
    def __init__(self, instances: dict[str, Instance], metrics: dict[str, MetricRecord],
                 quality: DataQualityReport | None = None) -> None:
        self._instances = dict(instances)
        self._metrics = dict(metrics)
        self.quality = quality or DataQualityReport()
        self._lock = threading.Lock()

    @classmethod
    def from_dir(cls, data_dir: Path) -> "CloudRepository":
        instances, metrics, quality = load_sources(data_dir)
        return cls(instances, metrics, quality)

    def list_instances(self, environment: str | None = None) -> list[Instance]:
        items = sorted(self._instances.values(), key=lambda i: i.instance_id)
        if environment:
            items = [i for i in items if i.environment == environment]
        return items

    def get_instance(self, instance_id: str) -> Instance:
        try:
            return self._instances[instance_id]
        except KeyError:
            raise InstanceNotFoundError(instance_id) from None

    def get_metrics(self, instance_id: str) -> MetricRecord | None:
        self.get_instance(instance_id)
        return self._metrics.get(instance_id)

    def stop_instance(self, instance_id: str) -> str:
        """Stop an instance. Idempotent: returns 'stopped' or 'already_stopped'."""
        with self._lock:
            inst = self.get_instance(instance_id)
            if inst.state == "stopped":
                return "already_stopped"
            self._instances[instance_id] = inst.model_copy(update={"state": "stopped"})
            return "stopped"
