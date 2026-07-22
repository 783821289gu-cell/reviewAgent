from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.errors import register_error_handlers
from api.routes import evaluation, feedback, health, local_review, reports, tasks
from config import Settings, settings
from db.repositories import ReviewPersistence
from services.event_service import ReviewEventStore
from services.review_service import ReviewOrchestratorAgent
from services.runtime_log_service import configure_runtime_logging


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"


def create_app(
    app_settings: Settings = settings,
    event_store: ReviewEventStore | None = None,
) -> FastAPI:
    configure_runtime_logging(
        app_settings.runtime_log_file,
        app_settings.runtime_log_level,
    )
    if event_store is None:
        event_store = ReviewEventStore(
            ReviewPersistence(app_settings.memory_db_path, app_settings.upload_dir)
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.accepting_tasks = False
        event_store.load_persisted()
        app.state.recovered_task_ids = app.state.review_agent.recover_pending_tasks()
        app.state.accepting_tasks = True
        try:
            yield
        finally:
            app.state.accepting_tasks = False
            event_store.notify_waiters()

    application = FastAPI(title="ContractReviewAgent", lifespan=lifespan)
    application.state.settings = app_settings
    application.state.event_store = event_store
    application.state.review_agent = ReviewOrchestratorAgent(
        event_store,
        node_timeout_seconds=app_settings.node_timeout_seconds,
        task_timeout_seconds=app_settings.task_timeout_seconds,
        deepseek_task_timeout_seconds=app_settings.deepseek_task_timeout_seconds,
        llm_max_concurrency=app_settings.llm_max_concurrency,
    )
    application.state.recovered_task_ids = []
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

    application.mount("/src", StaticFiles(directory=FRONTEND_DIR / "src"), name="frontend-src")

    @application.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    return application


app = create_app()
