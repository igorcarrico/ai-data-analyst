"""Execute validated SQL against SQLite and return results as DataFrames."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .database import get_connection
from .sql_validator import ValidationResult, validate_sql


@dataclass
class QueryResult:
    ok: bool
    sql: str
    dataframe: pd.DataFrame | None
    error: str | None = None

    @property
    def empty(self) -> bool:
        return self.dataframe is None or self.dataframe.empty


def run_query(sql: str) -> QueryResult:
    validation: ValidationResult = validate_sql(sql)
    if not validation.is_valid:
        return QueryResult(False, validation.cleaned_sql, None, validation.error)

    try:
        with get_connection() as conn:
            df = pd.read_sql_query(validation.cleaned_sql, conn)
    except Exception as exc:
        return QueryResult(False, validation.cleaned_sql, None, str(exc))

    return QueryResult(True, validation.cleaned_sql, df, None)
