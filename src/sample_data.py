"""Generate realistic synthetic sales data and seed the SQLite database."""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from .config import TABLE_NAME
from .database import ensure_schema, get_connection, table_is_empty

REGIOES = ["Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul"]
CANAIS = ["Online", "Loja Física", "Marketplace"]

PRODUTOS = [
    ("Notebook Pro 14", "Eletrônicos", 4500.0),
    ("Smartphone X", "Eletrônicos", 2800.0),
    ("Fone Bluetooth", "Eletrônicos", 320.0),
    ("Smart TV 50", "Eletrônicos", 2400.0),
    ("Camiseta Básica", "Vestuário", 79.9),
    ("Jaqueta Jeans", "Vestuário", 219.0),
    ("Tênis Runner", "Vestuário", 349.0),
    ("Café Premium 1kg", "Alimentos", 65.0),
    ("Chocolate Artesanal", "Alimentos", 28.0),
    ("Azeite Extra Virgem", "Alimentos", 55.0),
    ("Cafeteira Elétrica", "Casa", 389.0),
    ("Aspirador Robô", "Casa", 1599.0),
    ("Jogo de Panelas", "Casa", 499.0),
    ("Livro - Ficção", "Livros", 49.0),
    ("Livro - Técnico", "Livros", 120.0),
]

REGIAO_WEIGHTS = {
    "Sudeste": 0.42,
    "Sul": 0.20,
    "Nordeste": 0.18,
    "Centro-Oeste": 0.12,
    "Norte": 0.08,
}

CANAL_WEIGHTS = {
    "Online": 0.55,
    "Marketplace": 0.25,
    "Loja Física": 0.20,
}


def _weighted_choice(mapping: dict[str, float]) -> str:
    keys = list(mapping.keys())
    weights = list(mapping.values())
    return random.choices(keys, weights=weights, k=1)[0]


def _seasonal_multiplier(day: date) -> float:
    month = day.month
    if month in (11, 12):
        return 1.35
    if month in (1, 2):
        return 0.85
    if month in (6, 7):
        return 1.10
    return 1.0


def generate_rows(n_rows: int = 2500, seed: int = 42) -> list[tuple]:
    random.seed(seed)
    end = date.today()
    start = end - timedelta(days=365)
    span = (end - start).days

    rows: list[tuple] = []
    for i in range(1, n_rows + 1):
        day = start + timedelta(days=random.randint(0, span))
        produto, categoria, preco_base = random.choice(PRODUTOS)
        regiao = _weighted_choice(REGIAO_WEIGHTS)
        canal = _weighted_choice(CANAL_WEIGHTS)

        qtd = random.randint(1, 6)
        noise = random.uniform(0.9, 1.15)
        seasonal = _seasonal_multiplier(day)
        valor = round(preco_base * qtd * noise * seasonal, 2)

        rows.append(
            (
                i,
                day.isoformat(),
                regiao,
                produto,
                categoria,
                valor,
                qtd,
                canal,
            )
        )
    return rows


def seed_if_empty(db_path: Path | None = None, n_rows: int = 2500) -> int:
    """Create schema and populate with synthetic data if the table is empty.

    Returns the number of rows inserted (0 if already populated).
    """
    ensure_schema(db_path)
    if not table_is_empty(db_path):
        return 0

    rows = generate_rows(n_rows=n_rows)
    with get_connection(db_path) as conn:
        conn.executemany(
            f"INSERT INTO {TABLE_NAME} "
            "(id, data, regiao, produto, categoria, valor, quantidade, canal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)
