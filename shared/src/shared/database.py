import os
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)


def get_database_url() -> str:
    """
    Build the SQLAlchemy database URL from environment variables.

    UnifyRoute uses SQLite exclusively:
      - Uses SQLITE_PATH or data/unifyroute.db by default
      - DATABASE_URL can still override for advanced use cases
    """
    # Legacy / explicit DATABASE_URL passthrough
    explicit_url = os.environ.get("DATABASE_URL", "")
    if explicit_url:
        # Normalise sqlite:// → sqlite+aiosqlite://
        if explicit_url.startswith("sqlite://"):
            explicit_url = explicit_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        _ensure_sqlite_dir(explicit_url)
        return explicit_url

    # SQLite only
    sqlite_path = os.environ.get("SQLITE_PATH", "data/unifyroute.db")
    url = f"sqlite+aiosqlite:///{sqlite_path}"
    _ensure_sqlite_dir(url)
    return url


def _ensure_sqlite_dir(url: str):
    """Create the parent directory for a SQLite file if needed."""
    if "sqlite" in url:
        # Extract the file path portion: everything after the triple-slash
        path_part = url.split("///", 1)[-1]
        if path_part and path_part != ":memory:":
            parent = os.path.dirname(path_part)
            if parent:
                os.makedirs(parent, exist_ok=True)


_db_url = get_database_url()
logger.info("Database engine: %s", _db_url)
engine = create_async_engine(_db_url, echo=False)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session():
    """Dependency for FastAPI endpoints yielding an HTTP-request scoped Session."""
    async with async_session_maker() as session:
        yield session
