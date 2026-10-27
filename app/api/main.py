"""FastAPI application: thin HTTP layer over the orchestrator.

    uvicorn app.api.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from app.agent.orchestrator import InvalidRunStateError, Orchestrator
from app.agent.planner import make_planner
from app.agent.state import RunNotFoundError, RunState, RunStore
from app.api.schemas import ApprovalRequest, CreateRunRequest, Principal, RunSummary
from app.api.security import current_principal, require_operator
from app.config import Settings
from app.data.models import DataQualityReport
from app.data.repository import CloudRepository
from app.faults import ALL_FAULTS
from app.observability import configure_logging

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Settings | None = None, planner_factory=make_planner) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    repo = CloudRepository.from_dir(settings.data_dir)

    app = FastAPI(title="CloudOps Remediation Agent", version="0.1.0")
    app.state.settings = settings
    app.state.repo = repo
    app.state.orchestrator = Orchestrator(settings, repo, RunStore(settings.db_path), planner_factory)

    def orch(request: Request) -> Orchestrator:
        return request.app.state.orchestrator

    def get_run_or_404(run_id: str, o: Orchestrator) -> RunState:
        try:
            return o.store.get(run_id)
        except RunNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"run '{run_id}' not found") from None

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "planner": settings.planner, "data_errors": repo.quality.error_count}

    @app.get("/meta", tags=["ops"])
    def meta() -> dict:
        """Unauthenticated, non-sensitive config the UI needs to render."""
        return {"faults": list(ALL_FAULTS) if settings.allow_fault_injection else [],
                "fault_injection": settings.allow_fault_injection}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/runs", status_code=status.HTTP_201_CREATED, response_model=RunState, tags=["runs"])
    def create_run(body: CreateRunRequest, who: Principal = Depends(require_operator),
                   o: Orchestrator = Depends(orch)) -> RunState:
        if body.faults and not settings.allow_fault_injection:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "fault injection is disabled in this environment")
        return o.start(body.goal, who.user, body.faults)

    @app.get("/runs", response_model=list[RunSummary], tags=["runs"])
    def list_runs(limit: int = Query(20, ge=1, le=100), _: Principal = Depends(current_principal),
                  o: Orchestrator = Depends(orch)) -> list[RunSummary]:
        return [RunSummary(run_id=r.run_id, goal=r.goal, status=r.status, created_by=r.created_by,
                           created_at=r.created_at.isoformat(), proposed=len(r.proposal), summary=r.summary)
                for r in o.store.list(limit)]

    @app.get("/runs/{run_id}", response_model=RunState, tags=["runs"])
    def get_run(run_id: str, _: Principal = Depends(current_principal),
                o: Orchestrator = Depends(orch)) -> RunState:
        return get_run_or_404(run_id, o)

    @app.post("/runs/{run_id}/approval", response_model=RunState, tags=["runs"])
    def decide(run_id: str, body: ApprovalRequest, who: Principal = Depends(require_operator),
               o: Orchestrator = Depends(orch)) -> RunState:
        get_run_or_404(run_id, o)
        try:
            return o.decide(run_id, who.user, body.decision, body.instance_ids, body.comment)
        except InvalidRunStateError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/data-quality", response_model=DataQualityReport, tags=["data"])
    def data_quality(_: Principal = Depends(current_principal)) -> DataQualityReport:
        return repo.quality

    @app.get("/whoami", response_model=Principal, tags=["auth"])
    def whoami(who: Principal = Depends(current_principal)) -> Principal:
        return who

    return app


app = create_app()
