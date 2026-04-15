"""Auto-chart heuristics: pick a reasonable Plotly figure for a result DataFrame."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
from plotly.graph_objects import Figure

DATE_HINTS = ("data", "mes", "mês", "ano", "dia", "periodo", "período")


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series)


def _first_numeric(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        if _is_numeric(df[col]):
            return col
    return None


def _looks_like_date(col_name: str, series: pd.Series) -> bool:
    name = col_name.lower()
    if any(hint in name for hint in DATE_HINTS):
        return True
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    return False


def build_chart(df: pd.DataFrame) -> Figure | None:
    if df is None or df.empty:
        return None
    if len(df.columns) < 2:
        return None
    if len(df) > 500:
        return None

    numeric_col = _first_numeric(df)
    if numeric_col is None:
        return None

    non_numeric_cols = [c for c in df.columns if c != numeric_col and not _is_numeric(df[c])]
    if not non_numeric_cols:
        return None

    cat_col = non_numeric_cols[0]

    if _looks_like_date(cat_col, df[cat_col]):
        try:
            df_sorted = df.copy()
            df_sorted[cat_col] = df_sorted[cat_col].astype(str)
            df_sorted = df_sorted.sort_values(cat_col)
            fig = px.line(
                df_sorted,
                x=cat_col,
                y=numeric_col,
                markers=True,
                title=f"{numeric_col} por {cat_col}",
            )
            fig.update_layout(margin=dict(l=10, r=10, t=50, b=10))
            return fig
        except Exception:
            return None

    top = df.nlargest(min(20, len(df)), numeric_col)
    try:
        fig = px.bar(
            top,
            x=cat_col,
            y=numeric_col,
            title=f"{numeric_col} por {cat_col}",
            text_auto=".2s",
        )
        fig.update_layout(margin=dict(l=10, r=10, t=50, b=10), xaxis_tickangle=-30)
        return fig
    except Exception:
        return None
