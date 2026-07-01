from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import APP_TITLE
from .services.migrations import init_db
from .services.storage import ensure_dirs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    logger.info("Starting %s", APP_TITLE)
    ensure_dirs()
    init_db()
    logger.info("DB initialized")
    yield
    logger.info("Shutting down %s", APP_TITLE)


app = FastAPI(title=APP_TITLE, lifespan=_lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

from .routes.auth import router as auth_router
from .routes.admin import router as admin_router
from .routes.videos import router as videos_router
from .routes.tags import router as tags_router
from .routes.folders import router as folders_router
from .routes.hls import router as hls_router
from .routes.library import router as library_router
from .routes.settings import router as settings_router

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(videos_router)
app.include_router(tags_router)
app.include_router(folders_router)
app.include_router(hls_router)
app.include_router(library_router)
app.include_router(settings_router)
