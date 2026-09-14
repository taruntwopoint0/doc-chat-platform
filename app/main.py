"""FastAPI application entrypoint.

The worker runs as an asyncio task inside this process, started and stopped by
the lifespan hook. Render's Starter tier is one instance, so the API and the
worker share it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, chat, documents, health, jobs, workspaces
from app.config import get_settings
from app.db import dispose_engine
from app.registry import validate_wiring
from app.workers.worker import WorkerPool

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Fails fast on a misconfigured embedder width rather than deep inside a job.
    validate_wiring()
    # Without a first account there is no way to sign in to a fresh deploy.
    await auth.bootstrap_first_user()
    await auth.purge_expired_sessions()
    pool = WorkerPool()
    await pool.start()
    app.state.worker_pool = pool
    try:
        yield
    finally:
        await pool.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Document-grounded chatbot platform",
        description=(
            "Ingestion spine (Milestone 1): upload documents into a workspace, "
            "parse, profile, chunk, embed and index them. Retrieval and chat "
            "arrive in Milestone 2."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(workspaces.router)
    app.include_router(documents.router)
    app.include_router(jobs.router)
    app.include_router(chat.router)

    # The dashboard: three static files, no build step and no node_modules to
    # keep in step with the Python side.
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
