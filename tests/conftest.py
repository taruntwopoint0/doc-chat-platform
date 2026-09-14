"""Test fixtures.

The database-backed tests run against a real PostgreSQL with pgvector, started
in-process by `pgserver`. SQLite is not an option here -- the schema depends on
pgvector, JSONB, generated tsvector columns and SELECT ... FOR UPDATE SKIP
LOCKED, none of which SQLite has.

Set TEST_DATABASE_URL to point the suite at your own Postgres instead (the one
from docker-compose, for example).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Must be set before any app module reads Settings.
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("EMBEDDING_MODEL", "test-embedding-model")
os.environ.setdefault("LLM_MODEL", "test-llm-model")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("WORKER_ENABLED", "false")
# Sign-in is off by default in the app; the suite turns it on so the auth
# paths are actually exercised. The off path has its own tests.
os.environ.setdefault("AUTH_ENABLED", "true")

#: Every table with test state. New tables must be added here, or rows leak
#: between tests and failures become order-dependent.
_TABLES = (
    "workspaces, documents, document_files, chunks, ingest_jobs, "
    "users, sessions, chat_messages"
)

#: Name of the throwaway cluster directory under the system temp dir.
_TESTDB_DIRNAME = "doc-chat-platform-testdb"


def _start_pgserver() -> str:
    try:
        import pgserver
    except ImportError:  # pragma: no cover - optional dev dependency
        pytest.skip(
            "No database available. Install pgserver (pip install pgserver) or "
            "set TEST_DATABASE_URL to a Postgres with pgvector."
        )
    temp = Path(tempfile.gettempdir())
    data_dir = temp / _TESTDB_DIRNAME
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        return pgserver.get_server(data_dir, cleanup_mode=None).get_uri()
    except Exception as first_error:
        # A previous run's server was killed without shutting down, so the
        # cluster will not start. It is disposable test state: rebuild it.
        #
        # Not in place, though. pgserver keeps its log file *inside* the data
        # directory and a dead server's handle can keep it locked, so on Windows
        # rmtree leaves that one file behind -- and initdb refuses a non-empty
        # directory. A fresh sibling directory sidesteps the lock entirely.
        shutil.rmtree(data_dir, ignore_errors=True)
        fresh = Path(tempfile.mkdtemp(prefix=f"{_TESTDB_DIRNAME}-", dir=temp))
        try:
            return pgserver.get_server(fresh, cleanup_mode=None).get_uri()
        except Exception as second_error:
            raise RuntimeError(
                f"Could not start the test Postgres. In {data_dir}: "
                f"{first_error!r}. In a fresh {fresh}: {second_error!r}. "
                "Set TEST_DATABASE_URL to use your own Postgres instead."
            ) from second_error


@pytest.fixture(scope="session")
def database_url() -> str:
    """A migrated Postgres URL, in the app's async form."""
    url = os.environ.get("TEST_DATABASE_URL") or _start_pgserver()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    async_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    os.environ["DATABASE_URL"] = async_url

    from app.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    root = Path(__file__).resolve().parents[1]
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")
    return async_url


@pytest.fixture(autouse=True)
async def clean_database(request):
    """Empty every table between tests so each starts from a known state.

    The database is requested lazily, and only for tests that need one. Taking
    `database_url` as a parameter would start Postgres for pure tests too --
    so a database that cannot start would fail tests that never touch it.
    """
    if "no_db" in request.keywords:
        yield
        return
    request.getfixturevalue("database_url")
    from sqlalchemy import text

    from app.db import session_scope

    async with session_scope() as session:
        await session.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
async def session(database_url):
    from app.db import get_session_factory

    async with get_session_factory()() as s:
        yield s
        await s.commit()


@pytest.fixture
async def workspace(session):
    from uuid import uuid4

    from app.models.workspace import Workspace

    ws = Workspace(id=uuid4(), name="Test workspace", slug=f"ws-{uuid4().hex[:8]}",
                   config={}, profile={})
    session.add(ws)
    await session.commit()
    return ws


class FakeEmbedder:
    """Deterministic vectors, so a test can assert which chunks were embedded.

    Records every batch it was asked for, which is how the resumability tests
    check that a resumed run re-embeds nothing.
    """

    def __init__(self, dimensions: int = 768, fail_after: int | None = None) -> None:
        self._dimensions = dimensions
        self.batches: list[list[str]] = []
        #: Raise once this many texts have been embedded, to simulate a crash.
        self.fail_after = fail_after
        self.embedded = 0

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_batch(self, texts, task_type="document"):
        if self.fail_after is not None and self.embedded + len(texts) > self.fail_after:
            raise RuntimeError("simulated provider failure")
        self.batches.append(list(texts))
        self.embedded += len(texts)
        return [
            [float((hash(t) % 1000) / 1000.0)] * self._dimensions for t in texts
        ]


class FakeLLM:
    """Returns canned JSON, so profiling tests never touch the network."""

    def __init__(self, payload: str | None = None) -> None:
        self.payload = payload or (
            '{"document_type": "test document", "summary": "A test. Two sentences.",'
            ' "topics": ["alpha", "beta"], "entities": ["SystemX"]}'
        )
        self.calls: list[str] = []

    async def complete(self, prompt, system=None, json_mode=False, temperature=0.1):
        self.calls.append(prompt)
        return self.payload

    async def stream(self, prompt, system=None, temperature=0.1):
        yield self.payload


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()


@pytest.fixture
def fake_llm():
    return FakeLLM()
