"""Data-validation tests: the shipped fixtures and synthetic bad inputs."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.data.loader import SourceUnavailableError, load_inventory, load_metrics, load_sources
from app.data.models import DataQualityReport

HEADER = "instance_id,collected_at,avg_cpu_pct,max_cpu_pct,network_mb\n"
GOOD = {"instance_id": "i-dev-100", "name": "n", "environment": "dev", "owner": "o", "region": "r",
        "instance_type": "t3.micro", "hourly_cost_usd": 0.01, "state": "running"}


# --------------------------------------------------------------- shipped fixtures

def test_fixture_quality_report(settings):
    instances, metrics, report = load_sources(settings.data_dir)
    assert (report.inventory_total, report.inventory_valid) == (13, 12)
    assert (report.metrics_total, report.metrics_valid) == (14, 12)
    errors = [i for i in report.issues if i.severity == "error"]
    warnings = [i for i in report.issues if i.severity == "warning"]
    assert [e.location for e in errors if e.source == "inventory"] == ["record[12]"]
    assert len([e for e in errors if e.source == "metrics"]) == 2
    assert len(warnings) == 1 and "i-zzz-999" in warnings[0].message
    assert len(instances) == 12 and "i-zzz-999" not in metrics


def test_fixture_bad_rows_do_not_shadow_good_ones(settings):
    _, metrics, _ = load_sources(settings.data_dir)
    assert "i-dev-005" not in metrics            # only row has avg 'n/a'
    assert metrics["i-stg-001"].avg_cpu_pct == 22  # later row (-4%) rejected, earlier valid row kept
    assert metrics["i-dev-001"].avg_cpu_pct == 1.2  # latest of two valid rows
    assert "i-dev-006" not in metrics            # no metrics at all


def test_fixture_values_within_bounds(settings):
    instances, metrics, _ = load_sources(settings.data_dir)
    assert all(i.hourly_cost_usd >= 0 for i in instances.values())
    assert all(0 <= m.avg_cpu_pct <= m.max_cpu_pct <= 100 for m in metrics.values())
    assert all(m.collected_at.tzinfo is not None for m in metrics.values())
    assert set(metrics) <= set(instances)


# --------------------------------------------------------------- synthetic inputs

def _inv(tmp_path, data) -> tuple[dict, DataQualityReport]:
    p = tmp_path / "inventory.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    report = DataQualityReport()
    return load_inventory(p, report), report


def _met(tmp_path, body: str, known=("i-dev-100",)) -> tuple[dict, DataQualityReport]:
    p = tmp_path / "metrics.csv"
    p.write_text(body)
    report = DataQualityReport()
    return load_metrics(p, set(known), report), report


def test_duplicate_instance_keeps_first(tmp_path):
    instances, report = _inv(tmp_path, [GOOD, {**GOOD, "name": "dupe"}])
    assert instances["i-dev-100"].name == "n"
    assert "duplicate instance_id" in report.issues[0].message


@pytest.mark.parametrize("override, field", [
    ({"environment": "qa"}, "environment"),
    ({"hourly_cost_usd": -1}, "hourly_cost_usd"),
    ({"instance_id": "web-1"}, "instance_id"),
    ({"state": "terminated"}, "state"),
    ({"unexpected": 1}, "unexpected"),
])
def test_invalid_inventory_record_rejected(tmp_path, override, field):
    instances, report = _inv(tmp_path, [{**GOOD, **override}])
    assert instances == {} and field in report.issues[0].message


@pytest.mark.parametrize("row, msg", [
    ("i-dev-100,2026-10-25T00:00:00Z,30,10,5", "cannot exceed"),
    ("i-dev-100,2026-10-25T00:00:00,1,2,5", "timezone"),
    ("i-dev-100,2026-10-25T00:00:00Z,1,101,5", "max_cpu_pct"),
    ("i-dev-100,yesterday,1,2,5", "collected_at"),
    ("i-dev-100,2026-10-25T00:00:00Z,1,2,-5", "network_mb"),
])
def test_invalid_metric_row_rejected(tmp_path, row, msg):
    metrics, report = _met(tmp_path, HEADER + row + "\n")
    assert metrics == {} and msg in report.issues[0].message and report.issues[0].location == "line 2"


@pytest.mark.parametrize("content", ["{not json", json.dumps({"instances": []})])
def test_unreadable_inventory_raises(tmp_path, content):
    with pytest.raises(SourceUnavailableError):
        _inv(tmp_path, content)


def test_missing_sources_raise(tmp_path):
    with pytest.raises(SourceUnavailableError):
        load_sources(tmp_path)


def test_metrics_missing_column_raises(tmp_path):
    with pytest.raises(SourceUnavailableError, match="header"):
        _met(tmp_path, "instance_id,avg_cpu_pct\ni-dev-100,1\n")


# --------------------------------------------------------------- exposed via API

def test_data_quality_endpoint(settings):
    client = TestClient(create_app(replace(settings)))
    assert client.get("/data-quality").status_code == 401
    body = client.get("/data-quality", headers={"X-API-Key": "viewer-demo-key"}).json()
    assert body["inventory_valid"] == 12 and len(body["issues"]) == 4
    assert client.get("/healthz").json()["data_errors"] == 3
