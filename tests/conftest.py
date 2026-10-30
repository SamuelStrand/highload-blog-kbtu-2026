"""Integration fixtures: intentionally use PostgreSQL, never an SQLite substitute."""

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from blog_service.core.config import Settings
from blog_service.main import create_app
from blog_service.seed import seed_database
from blog_service.users.auth import issue_token


@pytest.fixture
def settings():
    value = Settings.load("test")
    if make_url(value.database_url).database != "sis2_blog_test":
        raise RuntimeError("Tests must use the dedicated sis2_blog_test database")
    return value


@pytest_asyncio.fixture
async def seeded(settings):
    return await seed_database(settings, reset=True, users=3, posts=6, comments=12)


@pytest_asyncio.fixture
async def app(settings, seeded):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def engine(app):
    return app.state.engine


@pytest.fixture
def session_factory(app):
    return app.state.session_factory


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def auth_headers(settings, seeded):
    return {"Authorization": f"Bearer {issue_token(seeded['user_id'], settings)}"}
