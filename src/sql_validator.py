"""SQL safety validator.

Rules enforced:
- Only a single statement is allowed.
- The statement must be a SELECT (or WITH ... SELECT).
- Destructive / DDL / attach keywords are rejected.
- Only the `vendas` table may be referenced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DML, Keyword

from .config import TABLE_NAME

FORBIDDEN_KEYWORDS = {
    "DELETE", "DROP", "UPDATE", "INSERT", "ALTER", "TRUNCATE",
    "CREATE", "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
    "GRANT", "REVOKE", "MERGE", "EXEC", "EXECUTE",
}

# Palavras que sinalizam intenção destrutiva na pergunta do usuário (pt/en).
DESTRUCTIVE_INTENT_PATTERNS = [
    # Verbos de remoção / alteração de dados
    r"\bapag\w*\b", r"\bdelet\w*\b", r"\bremov\w*\b", r"\bexclu\w*\b",
    r"\batualiz\w*\b", r"\balter\w*\b", r"\bmodif\w*\b", r"\bsobrescrev\w*\b",
    r"\binser\w*\b", r"\badicion\w*\b",
    r"\bzere?\b", r"\bzerar\b", r"\breset\w*\b", r"\blimpa?r?\b",
    r"\btruncar\b", r"\btrunca\b",
    # DDL: criar/dropar/alterar tabela (permitindo palavras no meio)
    r"\bcri\w*\b[^.?!]*\btabela\b",
    r"\bnov\w+\s+tabela\b",
    r"\bdrop\w*\b[^.?!]*\btabela\b",
    r"\balter\w*\b[^.?!]*\btabela\b",
    # Termos em inglês
    r"\bdrop\b", r"\bupdate\b", r"\binsert\b", r"\btruncate\b",
    r"\berase\b", r"\bwipe\b", r"\bcreate\s+table\b", r"\bnew\s+table\b",
]


def detect_destructive_intent(question: str) -> str | None:
    """Return a matched pattern if the user's question sounds destructive, else None."""
    if not question:
        return None
    q = question.lower()
    for pattern in DESTRUCTIVE_INTENT_PATTERNS:
        if re.search(pattern, q):
            return pattern
    return None


@dataclass
class ValidationResult:
    is_valid: bool
    cleaned_sql: str
    error: str | None = None


def _strip_code_fences(sql: str) -> str:
    sql = sql.strip()
    if sql.startswith("```"):
        sql = re.sub(r"^```[a-zA-Z]*\n?", "", sql)
        sql = re.sub(r"\n?```$", "", sql)
    return sql.strip().rstrip(";").strip()


def _contains_forbidden_keyword(sql_upper: str) -> str | None:
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", sql_upper):
            return kw
    return None


def _is_select_statement(stmt: Statement) -> bool:
    for token in stmt.tokens:
        if token.ttype is DML and token.value.upper() == "SELECT":
            return True
        if token.ttype is Keyword.CTE and token.value.upper() == "WITH":
            return True
        if token.ttype is Keyword and token.value.upper() == "WITH":
            return True
    return False


def _only_allowed_table(sql_upper: str, allowed: set[str] | None = None) -> bool:
    """Extract table names after FROM/JOIN and confirm they match the allowed set."""
    allowed_lower = {t.lower() for t in (allowed or {TABLE_NAME})}
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
    found = pattern.findall(sql_upper)
    if not found:
        return False
    return all(name.lower() in allowed_lower for name in found)


def validate_sql(sql: str, allowed_tables: set[str] | None = None) -> ValidationResult:
    if not sql or not sql.strip():
        return ValidationResult(False, "", "SQL vazio.")

    cleaned = _strip_code_fences(sql)
    if not cleaned:
        return ValidationResult(False, "", "SQL vazio após sanitização.")

    parsed = sqlparse.parse(cleaned)
    if len(parsed) == 0:
        return ValidationResult(False, cleaned, "Não foi possível interpretar o SQL.")
    if len(parsed) > 1:
        return ValidationResult(False, cleaned, "Múltiplas queries não são permitidas.")

    stmt = parsed[0]
    upper = cleaned.upper()

    forbidden = _contains_forbidden_keyword(upper)
    if forbidden:
        return ValidationResult(
            False, cleaned, f"Palavra-chave proibida detectada: {forbidden}."
        )

    if ";" in cleaned:
        return ValidationResult(False, cleaned, "Ponto-e-vírgula não é permitido no meio da query.")

    if not _is_select_statement(stmt):
        return ValidationResult(False, cleaned, "Apenas comandos SELECT são permitidos.")

    effective_tables = allowed_tables or {TABLE_NAME}
    if not _only_allowed_table(upper, effective_tables):
        names = ", ".join(f"`{t}`" for t in sorted(effective_tables))
        return ValidationResult(
            False, cleaned, f"Apenas a(s) tabela(s) {names} pode(m) ser consultada(s)."
        )

    return ValidationResult(True, cleaned, None)
