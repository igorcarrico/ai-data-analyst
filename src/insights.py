"""Generate textual insight summaries combining a rule-based fallback with LLM output."""
from __future__ import annotations

import pandas as pd

from .llm import LLMClient


MAX_ROWS_FOR_LLM = 30


def _rule_based_summary(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "A consulta não retornou linhas."

    parts: list[str] = [f"- Linhas retornadas: **{len(df)}**"]

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if numeric_cols:
        main = numeric_cols[0]
        total = df[main].sum()
        mean = df[main].mean()
        parts.append(f"- Soma de `{main}`: **{total:,.2f}**")
        parts.append(f"- Média de `{main}`: **{mean:,.2f}**")

        cat_cols = [c for c in df.columns if c not in numeric_cols]
        if cat_cols:
            cat = cat_cols[0]
            top_row = df.loc[df[main].idxmax()]
            parts.append(
                f"- Maior `{main}` em `{cat}` = **{top_row[cat]}** ({top_row[main]:,.2f})"
            )

    return "\n".join(parts)


def generate_insights(
    client: LLMClient,
    question: str,
    sql: str,
    df: pd.DataFrame,
) -> str:
    base = _rule_based_summary(df)
    if df is None or df.empty:
        return base

    try:
        sample_md = df.head(MAX_ROWS_FOR_LLM).to_markdown(index=False)
    except Exception:
        sample_md = df.head(MAX_ROWS_FOR_LLM).to_string(index=False)

    try:
        llm_text = client.generate_insights(question, sql, sample_md)
    except Exception as exc:
        return base + f"\n\n_(Insights via LLM indisponíveis: {exc})_"

    if not llm_text.strip():
        return base
    return f"{llm_text}\n\n---\n**Estatísticas rápidas**\n{base}"
