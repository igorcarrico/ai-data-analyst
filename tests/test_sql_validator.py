"""Security-focused tests for the SQL validator."""
from __future__ import annotations

import pytest

from src.sql_validator import validate_sql


VALID_CASES = [
    "SELECT * FROM vendas",
    "SELECT regiao, SUM(valor) FROM vendas GROUP BY regiao",
    "select produto, sum(quantidade) as qtd from vendas group by produto order by qtd desc limit 5",
    "WITH base AS (SELECT regiao, valor FROM vendas) SELECT regiao, SUM(valor) FROM vendas GROUP BY regiao",
]

INVALID_CASES = [
    ("DELETE FROM vendas", "DELETE"),
    ("DROP TABLE vendas", "DROP"),
    ("UPDATE vendas SET valor=0", "UPDATE"),
    ("INSERT INTO vendas VALUES (1)", "INSERT"),
    ("ALTER TABLE vendas ADD COLUMN foo TEXT", "ALTER"),
    ("TRUNCATE vendas", "TRUNCATE"),
    ("CREATE TABLE x (id INT)", "CREATE"),
    ("ATTACH DATABASE 'x' AS y", "ATTACH"),
    ("SELECT * FROM vendas; DROP TABLE vendas", "múltiplas"),
    ("SELECT * FROM usuarios", "vendas"),
    ("", "vazio"),
]


@pytest.mark.parametrize("sql", VALID_CASES)
def test_valid_select_queries(sql: str) -> None:
    result = validate_sql(sql)
    assert result.is_valid, f"Should be valid: {sql} -> {result.error}"


@pytest.mark.parametrize("sql, hint", INVALID_CASES)
def test_invalid_queries(sql: str, hint: str) -> None:
    result = validate_sql(sql)
    assert not result.is_valid, f"Should be invalid: {sql}"
    assert result.error is not None


def test_strips_markdown_fences() -> None:
    sql = "```sql\nSELECT * FROM vendas\n```"
    result = validate_sql(sql)
    assert result.is_valid
    assert "```" not in result.cleaned_sql


def test_rejects_semicolon_in_middle() -> None:
    result = validate_sql("SELECT 1; SELECT * FROM vendas")
    assert not result.is_valid
