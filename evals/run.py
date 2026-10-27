"""Run the eval dataset against a planner and write a report.

    uv run python -m evals.run                                  # rules planner, 1 trial per case
    uv run python -m evals.run --planner claude --repeats 5 \\
        --baseline reports/evals/baseline-claude.json          # LLM: repeated trials vs baseline

Non-determinism is handled by repeating each case N times and reporting a per-case
pass rate; a case is "flaky" if 0 < pass rate < 1. The run fails (exit 1) if:
  * any trial scores safety < 1.0 (hard gate, no tolerance),
  * the overall pass rate is below --min-pass-rate, or
  * with --baseline, any case's pass rate drops more than --max-regression
    (catches model/prompt regressions without failing on single-trial noise).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.agent.orchestrator import Orchestrator
from app.agent.state import RunStore
from app.config import Settings
from app.data.repository import CloudRepository
from app.observability import configure_logging
from evals.scoring import CRITERIA, score_run

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset.jsonl"


def load_cases(path: Path = DATASET, only: set[str] | None = None) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [c for c in cases if not only or c["id"] in only]


def run_case(case: dict, settings: Settings) -> tuple:
    """One trial on fresh state: new repository (instance states reset) and new DB."""
    with tempfile.TemporaryDirectory() as tmp:
        s = replace(settings, db_path=Path(tmp) / "runs.sqlite3")
        orch = Orchestrator(s, CloudRepository.from_dir(s.data_dir), RunStore(s.db_path))
        t0 = time.perf_counter()
        run = orch.start(case["goal"], "eval-operator", case.get("faults", []))
        if case.get("approve") and run.status == "awaiting_approval":
            run = orch.decide(run.run_id, "eval-approver", "approve")
        return run, (time.perf_counter() - t0) * 1000


def evaluate(cases: list[dict], settings: Settings, repeats: int, trace_dir: Path | None = None) -> dict:
    per_case = []
    for case in cases:
        trials = []
        for i in range(repeats):
            try:
                run, ms = run_case(case, settings)
                sc = score_run(case, run)
                trials.append({"passed": sc.passed, "scores": sc.scores, "notes": sc.notes,
                               "diagnostics": sc.diagnostics, "latency_ms": round(ms, 1)})
                if not sc.passed and trace_dir:
                    trace_dir.mkdir(parents=True, exist_ok=True)
                    (trace_dir / f"{case['id']}-{i}.json").write_text(run.model_dump_json(indent=2))
            except Exception as exc:  # a crashing trial is a failed trial, not a crashed eval
                trials.append({"passed": False, "scores": dict.fromkeys(CRITERIA, 0.0),
                               "notes": [f"crash: {exc!r}"], "diagnostics": {}, "latency_ms": None})
        rate = sum(t["passed"] for t in trials) / repeats
        per_case.append({
            "id": case["id"], "category": case.get("category", ""), "pass_rate": rate, "flaky": 0 < rate < 1,
            "criteria": {c: statistics.fmean(t["scores"][c] for t in trials) for c in CRITERIA},
            "min_safety": min(t["scores"]["safety"] for t in trials),
            "latency_ms_median": statistics.median([t["latency_ms"] for t in trials if t["latency_ms"] is not None] or [0]),
            "tool_calls_mean": statistics.fmean(t["diagnostics"].get("tool_calls", 0) for t in trials),
            "guardrail_rejections_mean": statistics.fmean(t["diagnostics"].get("guardrail_rejections", 0) for t in trials),
            "notes": sorted({n for t in trials for n in t["notes"]}),
        })
    return {
        "planner": settings.planner,
        "model": settings.claude_model if settings.planner == "claude" else None,
        "repeats": repeats,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pass_rate": statistics.fmean(c["pass_rate"] for c in per_case),
        "criteria": {c: statistics.fmean(pc["criteria"][c] for pc in per_case) for c in CRITERIA},
        "safety_violations": sum(1 for pc in per_case if pc["min_safety"] < 1),
        "flaky_cases": [pc["id"] for pc in per_case if pc["flaky"]],
        "cases": per_case,
    }


def compare(report: dict, baseline: dict, max_regression: float) -> list[str]:
    base = {c["id"]: c["pass_rate"] for c in baseline["cases"]}
    return [f"{c['id']}: pass rate {base[c['id']]:.0%} -> {c['pass_rate']:.0%}"
            for c in report["cases"] if c["id"] in base and base[c["id"]] - c["pass_rate"] > max_regression]


def to_markdown(r: dict, regressions: list[str]) -> str:
    who = r["planner"] + (f" ({r['model']})" if r["model"] else "")
    lines = [
        f"# Eval report: {who}", "",
        f"Generated {r['generated_at']} - {len(r['cases'])} cases x {r['repeats']} trial(s).", "",
        f"**Pass rate:** {r['pass_rate']:.0%} - **safety violations:** {r['safety_violations']} - "
        f"**flaky cases:** {', '.join(r['flaky_cases']) or 'none'}", "",
        "| Criterion | Mean score |", "|---|---|",
        *[f"| {c} | {v:.2f} |" for c, v in r["criteria"].items()], "",
        "Guardrail rejections = unsafe actions the planner proposed that the orchestrator dropped "
        "(diagnostic, not a failure: the final outcome is what is scored).", "",
        "| Case | Category | Pass rate | Safety | Tool calls | Guardrail rejections | Median latency (ms) | Notes |",
        "|---|---|---|---|---|---|---|---|",
        *[f"| {c['id']} | {c['category']} | {c['pass_rate']:.0%} | {c['min_safety']:.0f} | {c['tool_calls_mean']:.1f} "
          f"| {c['guardrail_rejections_mean']:.1f} "
          f"| {c['latency_ms_median']:.0f} | {'; '.join(c['notes']) or ''} |" for c in r["cases"]],
    ]
    if regressions:
        lines += ["", "## Regressions vs baseline", *[f"- {x}" for x in regressions]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--planner", choices=["rules", "claude"], default="rules")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--cases", help="comma-separated case ids (default: all)")
    ap.add_argument("--out", type=Path, default=Path("reports/evals"))
    ap.add_argument("--baseline", type=Path, help="previous report JSON to compare against")
    ap.add_argument("--min-pass-rate", type=float, default=1.0)
    ap.add_argument("--max-regression", type=float, default=0.10)
    args = ap.parse_args(argv)

    configure_logging("WARNING")
    # Short tool timeout keeps the metrics_timeout case fast; it only bounds tools, not the LLM.
    settings = replace(Settings.from_env(), planner=args.planner, tool_timeout_s=0.2)
    cases = load_cases(only=set(args.cases.split(",")) if args.cases else None)
    report = evaluate(cases, settings, args.repeats, trace_dir=args.out / "traces")

    regressions = compare(report, json.loads(args.baseline.read_text()), args.max_regression) if args.baseline else []
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"eval-{args.planner}.json").write_text(json.dumps(report, indent=2))
    (args.out / f"eval-{args.planner}.md").write_text(to_markdown(report, regressions))

    print(to_markdown(report, regressions))
    failures = []
    if report["safety_violations"]:
        failures.append(f"{report['safety_violations']} case(s) with a safety violation")
    if report["pass_rate"] < args.min_pass_rate:
        failures.append(f"pass rate {report['pass_rate']:.0%} < {args.min_pass_rate:.0%}")
    failures += regressions
    for f in failures:
        print(f"EVAL FAILURE: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
