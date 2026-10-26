"""Load and validate the raw sources.

Bad records never crash the load: each one is rejected individually and
reported in a DataQualityReport so operators can see what was dropped.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from pydantic import ValidationError

from app.data.models import DataIssue, DataQualityReport, Instance, MetricRecord


class SourceUnavailableError(RuntimeError):
    """A whole source could not be read (missing file, unparseable document)."""


def _fmt(err: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['msg']}" for e in err.errors())


def load_inventory(path: Path, report: DataQualityReport) -> dict[str, Instance]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise SourceUnavailableError(f"inventory source missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceUnavailableError(f"inventory is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise SourceUnavailableError("inventory must be a JSON array of instances")

    instances: dict[str, Instance] = {}
    report.inventory_total = len(raw)
    for idx, record in enumerate(raw):
        try:
            inst = Instance.model_validate(record)
        except ValidationError as exc:
            report.issues.append(DataIssue(source="inventory", location=f"record[{idx}]", severity="error", message=_fmt(exc)))
            continue
        if inst.instance_id in instances:
            report.issues.append(DataIssue(source="inventory", location=f"record[{idx}]", severity="error",
                                           message=f"duplicate instance_id {inst.instance_id}; first record kept"))
            continue
        instances[inst.instance_id] = inst
    report.inventory_valid = len(instances)
    return instances


def load_metrics(path: Path, known_ids: set[str], report: DataQualityReport) -> dict[str, MetricRecord]:
    """Return the most recent valid metric record per known instance."""
    try:
        handle = path.open(newline="")
    except FileNotFoundError as exc:
        raise SourceUnavailableError(f"metrics source missing: {path}") from exc

    latest: dict[str, MetricRecord] = {}
    valid = 0
    with handle:
        reader = csv.DictReader(handle)
        expected = {"instance_id", "collected_at", "avg_cpu_pct", "max_cpu_pct", "network_mb"}
        if not reader.fieldnames or not expected.issubset(reader.fieldnames):
            raise SourceUnavailableError(f"metrics header must contain {sorted(expected)}")
        for row in reader:
            report.metrics_total += 1
            line = f"line {reader.line_num}"
            try:
                rec = MetricRecord.model_validate(row)
            except ValidationError as exc:
                report.issues.append(DataIssue(source="metrics", location=line, severity="error", message=_fmt(exc)))
                continue
            valid += 1
            if rec.instance_id not in known_ids:
                report.issues.append(DataIssue(source="metrics", location=line, severity="warning",
                                               message=f"metrics for unknown instance {rec.instance_id} ignored"))
                continue
            current = latest.get(rec.instance_id)
            if current is None or rec.collected_at > current.collected_at:
                latest[rec.instance_id] = rec
    report.metrics_valid = valid
    return latest


def load_sources(data_dir: Path) -> tuple[dict[str, Instance], dict[str, MetricRecord], DataQualityReport]:
    report = DataQualityReport()
    instances = load_inventory(data_dir / "inventory.json", report)
    metrics = load_metrics(data_dir / "metrics.csv", set(instances), report)
    return instances, metrics, report
