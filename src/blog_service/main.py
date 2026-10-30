"""Application composition; domain behavior lives in each application's package."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from blog_service.comments.router import router as comments_router
from blog_service.core.config import Settings
from blog_service.core.database import create_engine, get_session, session_factory
from blog_service.core.errors import register_error_handlers
from blog_service.core.model_registry import Base  # noqa: F401 -- register every domain model
from blog_service.core.schemas import ErrorResponse
from blog_service.posts.router import router as posts_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    engine = create_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="SIS-2 Blog Platform",
        version="0.1.0",
        lifespan=lifespan,
        description="The same REST/JSON API served over HTTP/1.1 and HTTP/2.",
        responses={status: {"model": ErrorResponse} for status in (400, 401, 404, 409, 422, 503)},
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    register_error_handlers(app)
    app.include_router(posts_router)
    app.include_router(comments_router)

    @app.get("/health", tags=["operations"])
    async def health(session: AsyncSession = Depends(get_session, scope="function")):
        database = await session.scalar(text("SELECT current_database()"))
        return {"status": "ok", "database": database}

    return app
