# Test report

Generated from `uv run pytest --junitxml=reports/junit.xml --cov=app --cov=evals` on
2026-10-26 (macOS, Python 3.12.8). CI regenerates `junit.xml` / `coverage.xml` on every
run and uploads them as artifacts.

**Result: 145 passed, 4 skipped, 0 failed in 9.9 s. Line coverage 95 %.**

| Layer | Tests | What it covers |
|---|---|---|
| Unit (`tests/unit`) | 65 | Guardrails (goal policy, injection, prod protection, grounding), tool schemas, timeouts, retries, fault behavior, planner goal parsing, config |
| Data validation (`tests/data`) | 19 | Shipped fixtures (counts, bounds, timezone), synthetic bad inventory/metrics rows, unreadable sources, `/data-quality` |
| API (`tests/api`) | 14 | Success path, request validation, 401/403 auth, 404/409 errors, partial approval, failure statuses |
| Integration (`tests/integration`) | 20 | Orchestrator + tools + guardrails + SQLite with a scripted "misbehaving" planner: unsafe proposals dropped, step budget, planner errors, restart-safe approval, concurrent approvals execute once, persistent cloud errors |
| Evals (`tests/evals`) | 25 + 4 skipped | All 15 dataset cases pass; the scorer fails deliberately bad runs (prod proposal, ungrounded evidence, out-of-scope listing, missing findings, execution without approval, silent success on outage); regression comparison; CLI gate |
| Browser (`tests/e2e`) | 2 | Playwright against a live uvicorn: operator starts a run with an injected 429, reviews, approves, sees results; viewer is refused |

Skipped: `tests/evals/test_evals.py::test_claude_planner_is_safe` (4 cases), marked `llm`,
needs `ANTHROPIC_API_KEY`. Runs in the nightly and deploy workflows.

## Coverage

| Module | Cover | Notes |
|---|---|---|
| app/agent/guardrails.py | 100 % | |
| app/agent/orchestrator.py | 98 % | |
| app/agent/tools.py | 98 % | |
| app/agent/state.py | 99 % | |
| app/agent/planner.py | 69 % | The uncovered lines are `ClaudePlanner` API calls, exercised only with a key |
| app/api/* | 94-100 % | |
| app/data/* | 97-100 % | |
| evals/scoring.py | 97 % | |
| evals/run.py | 92 % | |
| **Total** | **95 %** | |

## Screenshots (from the browser test)

- [`screenshots/01-awaiting-approval.png`](screenshots/01-awaiting-approval.png): proposal with evidence, the redacted tag injection, data-quality findings and the agent trace
- [`screenshots/02-approved.png`](screenshots/02-approved.png): after approval, all three stops succeeded despite the injected 429
- [`screenshots/03-viewer-forbidden.png`](screenshots/03-viewer-forbidden.png): a read-only user cannot start a run

## Eval report

See [`evals/eval-rules.md`](evals/eval-rules.md): 15 cases, 100 % pass, 0 safety violations.
