# CloudOps Remediation Agent

Human-in-the-loop AI agent that finds idle cloud VMs and proposes stopping them.
Nothing is changed until an operator approves.

## Run

```bash
uv sync
uv run uvicorn app.api.main:app --reload   # UI at http://localhost:8000/, API docs at /docs
```

Demo API keys (`X-API-Key` header): `operator-demo-key` (bob, can start/approve runs),
`viewer-demo-key` (alice, read-only).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLOUDOPS_PLANNER` | `auto` | `rules` (deterministic), `claude`, or `auto` (Claude if `ANTHROPIC_API_KEY` is set) |
| `CLOUDOPS_CLAUDE_MODEL` | `claude-opus-5-5` | Model used by the Claude planner |
| `CLOUDOPS_DB_PATH` | `var/runs.sqlite3` | Run history store |
| `CLOUDOPS_ALLOW_FAULT_INJECTION` | `true` | Set `false` in production |
| `CLOUDOPS_API_KEYS` | demo keys | `key:user:role,...` |
| `CLOUDOPS_AS_OF` | `2026-10-26T00:00Z` | Pinned "now" for staleness checks on the fixture data |

## Run statuses

- `awaiting_approval`: idle instances proposed. Approve all, approve a subset, or reject.
- `completed`: nothing qualified, or the approved stops were executed.
- `failed`: the run is inconclusive (no instance could be assessed, e.g. metrics down
  or all stale), the planner errored, or every approved stop failed.
- `rejected` / `blocked`: rejected by the operator, or the goal was refused by policy.

## Test

```bash
uv run pytest
```

Fault injection (`faults` on `POST /runs`, or the checkboxes in the UI):
`metrics_unavailable`, `metrics_timeout`, `invalid_tool_response`, `stale_data`,
`cloud_api_error_once`. Test them one at a time. `metrics_unavailable` stops anything
from being proposed, so `stale_data` and `cloud_api_error_once` never take effect
when combined with it.
