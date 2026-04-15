"""Central configuration loaded from environment variables or Streamlit secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _get_secret(key: str, default: str | None = None) -> str | None:
    """Read a secret from env vars first, then Streamlit secrets (cloud deploy)."""
    value = os.getenv(key)
    if value:
        return value
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_model: str
    openai_api_key: str | None
    anthropic_api_key: str | None
    database_path: Path
    log_level: str
    log_file: Path

    @property
    def has_llm_credentials(self) -> bool:
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return False


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_settings() -> Settings:
    provider = (_get_secret("LLM_PROVIDER") or "anthropic").strip().lower()
    default_model = "gpt-4o-mini" if provider == "openai" else "claude-haiku-4-5-20251001"
    model = (_get_secret("LLM_MODEL") or default_model).strip()

    database_path = _resolve_path(_get_secret("DATABASE_PATH") or "data/database.db")
    log_file = _resolve_path("logs/query_logs.csv")

    return Settings(
        llm_provider=provider,
        llm_model=model,
        openai_api_key=_get_secret("OPENAI_API_KEY"),
        anthropic_api_key=_get_secret("ANTHROPIC_API_KEY"),
        database_path=database_path,
        log_level=(_get_secret("LOG_LEVEL") or "INFO").upper(),
        log_file=log_file,
    )


SETTINGS = load_settings()

TABLE_NAME = "vendas"

TABLE_SCHEMA = {
    "id": "INTEGER PRIMARY KEY",
    "data": "DATE",
    "regiao": "TEXT",
    "produto": "TEXT",
    "categoria": "TEXT",
    "valor": "REAL",
    "quantidade": "INTEGER",
    "canal": "TEXT",
}

SCHEMA_DESCRIPTION = """
Tabela: vendas
Colunas:
- id (INTEGER)        : identificador único da venda
- data (DATE)         : data da venda no formato YYYY-MM-DD
- regiao (TEXT)       : região do Brasil (Norte, Nordeste, Centro-Oeste, Sudeste, Sul)
- produto (TEXT)      : nome do produto vendido
- categoria (TEXT)    : categoria do produto (Eletrônicos, Vestuário, Alimentos, Casa, Livros)
- valor (REAL)        : valor total da venda em reais (preço unitário * quantidade)
- quantidade (INTEGER): quantidade vendida
- canal (TEXT)        : canal de venda (Online, Loja Física, Marketplace)
""".strip()
