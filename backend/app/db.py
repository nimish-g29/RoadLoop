from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def init_db() -> None:
    from . import models  # noqa: F401  register tables

    if is_postgres():
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    Base.metadata.create_all(engine)
    if is_postgres():
        raw = (Path(__file__).resolve().parent.parent / "sql" / "001_postgis.sql").read_text()
        # Drop "--" comment lines first: a ';' inside a comment would split a statement.
        sql = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
        with engine.begin() as conn:
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                conn.execute(text(stmt))


def get_session():
    with SessionLocal() as s:
        yield s
