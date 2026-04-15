"""End-to-end tests against an isolated SQLite database."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # Reload config + dependent modules to pick up env change
    for mod in [
        "src.query_executor",
        "src.database",
        "src.sample_data",
        "src.config",
    ]:
        sys.modules.pop(mod, None)

    from src.sample_data import seed_if_empty
    seed_if_empty(n_rows=300)
    yield


def test_run_query_returns_dataframe():
    from src.query_executor import run_query
    result = run_query("SELECT regiao, SUM(valor) AS total FROM vendas GROUP BY regiao")
    assert result.ok, result.error
    assert result.dataframe is not None
    assert not result.dataframe.empty
    assert "regiao" in result.dataframe.columns
    assert "total" in result.dataframe.columns


def test_run_query_rejects_destructive():
    from src.query_executor import run_query
    result = run_query("DELETE FROM vendas")
    assert not result.ok
    assert result.error is not None


def test_run_query_rejects_other_tables():
    from src.query_executor import run_query
    result = run_query("SELECT * FROM sqlite_master")
    assert not result.ok


def test_run_query_handles_invalid_sql():
    from src.query_executor import run_query
    result = run_query("SELECT * FROM vendas WHERE nonexistent_col = 1")
    assert not result.ok
    assert result.error is not None
