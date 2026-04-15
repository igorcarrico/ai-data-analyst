"""SQLite connection and bootstrap helpers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import SETTINGS, TABLE_NAME, TABLE_SCHEMA


def _build_create_stmt() -> str:
    cols = ", ".join(f"{name} {dtype}" for name, dtype in TABLE_SCHEMA.items())
    return f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} ({cols})"


@contextmanager
def get_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or SETTINGS.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema(db_path: Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(_build_create_stmt())


def table_is_empty(db_path: Path | None = None) -> bool:
    with get_connection(db_path) as conn:
        cur = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
        return cur.fetchone()[0] == 0
