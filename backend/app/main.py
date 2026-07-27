from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.errors import register_error_handlers
from api.routes import evaluation, feedback, health, local_review, reports, tasks
from config import Settings, settings
from services.application_runtime import (
    ApplicationRuntime,
    build_application_runtime,
)
from services.event_service import ReviewEventStore
from services.event_notification import PersistentEventStream
from services.mcp_service import create_mcp_runtime
from services.observability_service import (
    configure_observability,
    shutdown_observability,
)
from services.review_service import ReviewOrchestratorAgent
from services.runtime_log_service import (
    close_runtime_logging,
    configure_runtime_logging,
    write_runtime_log,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"


def create_app(
    app_settings: Settings = settings,
    event_store: ReviewEventStore | None = None,
    persistent_event_stream: PersistentEventStream | None = None,
) -> FastAPI:
    configure_runtime_logging(
        app_settings.runtime_log_file,
        app_settings.runtime_log_level,
    )
    mcp_runtime = create_mcp_runtime(app_settings)
    managed_runtime = event_store is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.accepting_tasks = False
        async with AsyncExitStack() as stack:
            stack.callback(_close_runtime_resources, app)
            try:
                app.state.observability = configure_observability(app_settings)
                if mcp_runtime is not None:
                    await stack.enter_async_context(
                        mcp_runtime.server.session_manager.run()
                    )
                if managed_runtime:
                    runtime = None
                    try:
                        runtime = build_application_runtime(app_settings)
                        runtime.event_store.load_persisted(
                            clear_execution_leases=False
                        )
                    except Exception as exc:
                        if runtime is not None:
                            try:
                                runtime.close()
                            except Exception:
                                pass
                        app.state.runtime_dependency_error = (
                            "external runtime initialization failed "
                            f"({exc.__class__.__name__})"
                        )
                        write_runtime_log(
                            "application_runtime_unavailable",
                            error_type=exc.__class__.__name__,
                        )
                        yield
                        return
                    app.state.runtime = runtime
                    app.state.event_store = runtime.event_store
                    app.state.persistent_event_stream = (
                        runtime.persistent_event_stream
                    )
                    app.state.queued_review_service = (
                        runtime.queued_review_service
                    )
                    app.state.review_agent = runtime.review_agent
                else:
                    loaded_states = app.state.event_store.load_persisted()
                    app.state.materialized_evidence_task_ids = (
                        app.state.review_agent.materialize_legacy_evidence_failures(
                            loaded_states
                        )
                    )
                    app.state.recovered_task_ids = (
                        app.state.review_agent.recover_pending_tasks()
                    )
                app.state.runtime_dependency_error = ""
                app.state.accepting_tasks = True
                yield
            finally:
                app.state.accepting_tasks = False
                active_event_store = app.state.event_store
                if active_event_store is not None:
                    active_event_store.notify_waiters()
                app.state.observability = None

    application = FastAPI(title="ContractReviewAgent", lifespan=lifespan)
    application.state.settings = app_settings
    application.state.event_store = event_store
    application.state.persistent_event_stream = persistent_event_stream
    application.state.queued_review_service = None
    application.state.runtime = None
    application.state.runtime_dependency_error = ""
    application.state.observability = None
    application.state.mcp_runtime = mcp_runtime
    application.state.review_agent = (
        None
        if managed_runtime
        else ReviewOrchestratorAgent(
            event_store,
            node_timeout_seconds=app_settings.node_timeout_seconds,
            llm_max_concurrency=app_settings.llm_max_concurrency,
        )
    )
    application.state.recovered_task_ids = []
    application.state.materialized_evidence_task_ids = []
    application.state.accepting_tasks = False

    if app_settings.allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(app_settings.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

    register_error_handlers(application)
    application.include_router(health.router)
    application.include_router(tasks.router)
    application.include_router(feedback.router)
    application.include_router(reports.router)
    application.include_router(local_review.router)
    application.include_router(evaluation.router)
    if mcp_runtime is not None:
        application.mount("/mcp", mcp_runtime.application, name="mcp")

    application.mount("/src", StaticFiles(directory=FRONTEND_DIR / "src"), name="frontend-src")

    @application.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    return application


def _close_runtime_resources(app: FastAPI) -> None:
    try:
        runtime: ApplicationRuntime | None = app.state.runtime
        if runtime is not None:
            runtime.close()
        elif app.state.review_agent is not None:
            app.state.review_agent.close()
    finally:
        try:
            shutdown_observability()
        finally:
            close_runtime_logging()


app = create_app()
