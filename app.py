"""AI Data Analyst - Streamlit UI.

Orchestrates: NL question -> LLM SQL -> validator -> executor -> table + chart + insights.
"""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime
from hashlib import sha1
from io import BytesIO

import pandas as pd
import streamlit as st

from src.charts import build_chart
from src.config import SCHEMA_DESCRIPTION, SETTINGS, TABLE_NAME
from src.database import load_dataframe
from src.insights import generate_insights
from src.llm import build_client
from src.logger import log_interaction, logger
from src.query_executor import QueryResult, run_query
from src.sample_data import seed_if_empty
from src.sql_validator import detect_destructive_intent, validate_sql

PAGE_TITLE = "AI Data Analyst"
PAGE_ICON = "📊"
DEMO_QUESTION_LIMIT = 30

EXAMPLE_QUESTIONS = [
    "Qual o total de vendas por região?",
    "Quais os 5 produtos mais vendidos em valor?",
    "Como foi a evolução mensal das vendas no último ano?",
    "Qual canal de vendas teve melhor desempenho?",
    "Qual a categoria com maior ticket médio?",
    "Quantas vendas foram feitas no Sudeste?",
]


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def _bootstrap() -> dict:
    inserted = seed_if_empty()
    client = build_client()
    return {"inserted": inserted, "client": client}


def _init_session_state() -> None:
    st.session_state.setdefault("history", [])
    st.session_state.setdefault("response_cache", {})
    st.session_state.setdefault("pending_question", "")
    st.session_state.setdefault("user_api_key", "")
    st.session_state.setdefault("question_count", 0)


def _get_active_client():
    """Prefer the user's own API key when provided; else fall back to the maintainer client."""
    user_key = st.session_state.get("user_api_key", "").strip()
    if user_key:
        if SETTINGS.llm_provider == "anthropic":
            from src.llm import AnthropicClient
            return AnthropicClient(SETTINGS.llm_model, user_key)
        if SETTINGS.llm_provider == "openai":
            from src.llm import OpenAIClient
            return OpenAIClient(SETTINGS.llm_model, user_key)
    return _bootstrap()["client"]


def _can_call_llm() -> bool:
    if st.session_state.get("user_api_key", "").strip():
        return True
    return st.session_state.get("question_count", 0) < DEMO_QUESTION_LIMIT


def _record_llm_call() -> None:
    if not st.session_state.get("user_api_key", "").strip():
        st.session_state["question_count"] = st.session_state.get("question_count", 0) + 1


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def _cache_key(question: str) -> str:
    ctx = st.session_state.get("upload_context")
    if ctx:
        prefix = "|".join(sorted(ctx["allowed_tables"]))
    else:
        prefix = TABLE_NAME
    raw = f"{prefix}:{question.strip().lower()}"
    return sha1(raw.encode("utf-8")).hexdigest()


def _dataframe_to_xlsx(df: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Resultado")
    return buffer.getvalue()


def _build_schema_from_df(df: pd.DataFrame, table_name: str) -> str:
    """Auto-discover schema from a DataFrame for the LLM prompt."""
    lines = [f"Tabela: {table_name}", "Colunas:"]
    for col in df.columns:
        dtype = df[col].dtype
        if pd.api.types.is_integer_dtype(dtype):
            sql_type = "INTEGER"
        elif pd.api.types.is_float_dtype(dtype):
            sql_type = "REAL"
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            sql_type = "DATE"
        else:
            sql_type = "TEXT"
        sample = df[col].dropna().unique()[:5]
        examples = ", ".join(str(v) for v in sample)
        lines.append(f"- {col} ({sql_type}): ex.: {examples}")
    return "\n".join(lines)


def _sanitize_table_name(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem)
    name = re.sub(r"_+", "_", name).strip("_")
    return name.lower() or "tabela"


def _normalize_column_name(name: str) -> str:
    """Make a column name SQL-safe: lowercase, ASCII-only, no spaces."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower() or "col"


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename columns in-place to SQL-safe identifiers, deduplicating collisions."""
    seen: dict[str, int] = {}
    new_cols: list[str] = []
    for c in df.columns:
        norm = _normalize_column_name(c)
        if norm in seen:
            seen[norm] += 1
            norm = f"{norm}_{seen[norm]}"
        else:
            seen[norm] = 1
        new_cols.append(norm)
    df.columns = new_cols
    return df


def _build_chart_png(df: pd.DataFrame) -> bytes | None:
    """Render a chart as PNG with matplotlib.

    Uses the same heuristics as src.charts.build_chart (bar for categorical,
    line for date-like), but doesn't depend on a headless browser, so it works
    on any container — unlike kaleido, which needs Chromium.
    """
    if df is None or df.empty or len(df.columns) < 2 or len(df) > 500:
        return None

    numeric_col = next(
        (c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])), None,
    )
    if numeric_col is None:
        return None

    non_numeric_cols = [
        c for c in df.columns
        if c != numeric_col and not pd.api.types.is_numeric_dtype(df[c])
    ]
    if not non_numeric_cols:
        return None
    cat_col = non_numeric_cols[0]

    date_hints = ("data", "mes", "mês", "ano", "dia", "periodo", "período")
    is_date = (
        any(h in cat_col.lower() for h in date_hints)
        or pd.api.types.is_datetime64_any_dtype(df[cat_col])
    )

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5), dpi=120)
    try:
        if is_date:
            df_sorted = df.sort_values(cat_col)
            ax.plot(
                df_sorted[cat_col].astype(str),
                df_sorted[numeric_col],
                marker="o",
                color="#1f77b4",
            )
        else:
            top = df.nlargest(min(20, len(df)), numeric_col)
            bars = ax.bar(top[cat_col].astype(str), top[numeric_col], color="#1f77b4")
            for bar, val in zip(bars, top[numeric_col]):
                ax.annotate(
                    f"{val:,.0f}".replace(",", "."),
                    xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 3),
                    textcoords="offset points",
                    ha="center",
                    fontsize=8,
                )

        ax.set_xlabel(cat_col)
        ax.set_ylabel(numeric_col)
        ax.set_title(f"{numeric_col} por {cat_col}")
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()

        buffer = BytesIO()
        fig.savefig(buffer, format="png", dpi=120, bbox_inches="tight")
        return buffer.getvalue()
    finally:
        plt.close(fig)


def _build_pdf_report(payload: dict) -> bytes:
    """Assemble a PDF with question, SQL, chart image, table preview and insights."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Image as RLImage,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        leftMargin=1.8 * cm,
        rightMargin=1.8 * cm,
        title="AI Data Analyst - Relatorio",
    )

    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h2_style = styles["Heading2"]
    body_style = styles["BodyText"]
    code_style = ParagraphStyle(
        "SQLCode",
        parent=styles["Code"],
        fontSize=9,
        leading=12,
        backColor=colors.whitesmoke,
        borderPadding=6,
    )

    story: list = []
    story.append(Paragraph("AI Data Analyst - Relatorio", title_style))
    story.append(Paragraph(
        f"Gerado em {datetime.now():%Y-%m-%d %H:%M}", body_style,
    ))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Pergunta", h2_style))
    story.append(Paragraph(payload["question"], body_style))
    story.append(Spacer(1, 8))

    story.append(Paragraph("SQL gerada", h2_style))
    story.append(Preformatted(payload["sql"], code_style))
    story.append(Spacer(1, 8))

    df = payload.get("dataframe")
    if df is not None and not df.empty:
        try:
            img_bytes = _build_chart_png(df)
            if img_bytes:
                story.append(Paragraph("Visualizacao", h2_style))
                story.append(RLImage(BytesIO(img_bytes), width=16 * cm, height=9 * cm))
                story.append(Spacer(1, 8))
        except Exception as exc:
            logger.warning("pdf chart embed failed: %s", exc)

        story.append(Paragraph("Resultado", h2_style))
        preview = df.head(50)
        table_data = [list(preview.columns)] + preview.astype(str).values.tolist()
        table = Table(table_data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4A4A4A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(table)
        if len(df) > 50:
            story.append(Paragraph(
                f"<i>(Preview: 50 de {len(df)} linhas. Para dataset completo, baixe CSV/XLSX.)</i>",
                body_style,
            ))
        story.append(Spacer(1, 8))

    if payload.get("insights"):
        story.append(Paragraph("Insights", h2_style))
        insights_html = payload["insights"].replace("\n", "<br/>")
        story.append(Paragraph(insights_html, body_style))

    doc.build(story)
    return buffer.getvalue()


def _get_pdf_bytes(payload: dict) -> bytes | None:
    """Lazy-build the PDF once and cache it in the payload dict."""
    if payload.get("pdf_bytes") is not None:
        return payload["pdf_bytes"]
    try:
        payload["pdf_bytes"] = _build_pdf_report(payload)
        return payload["pdf_bytes"]
    except Exception as exc:
        logger.exception("pdf build failed")
        payload["pdf_error"] = str(exc)
        return None


def _render_pdf_button(payload: dict, key_prefix: str) -> None:
    """Two-step button: 'Gerar PDF' triggers build, then download button appears."""
    pdf_ready_key = f"pdf_ready_{key_prefix}"

    if st.session_state.get(pdf_ready_key) and payload.get("pdf_bytes"):
        st.download_button(
            label="⬇️ Baixar PDF",
            data=payload["pdf_bytes"],
            file_name=f"relatorio_{key_prefix}.pdf",
            mime="application/pdf",
            key=f"dl_pdf_{key_prefix}",
            width="stretch",
        )
        return

    if st.button("📄 Gerar PDF", key=f"gen_pdf_{key_prefix}", width="stretch"):
        with st.spinner("Montando relatório (pode levar alguns segundos)..."):
            pdf_bytes = _get_pdf_bytes(payload)
        if pdf_bytes:
            st.session_state[pdf_ready_key] = True
            st.rerun()
        else:
            err = payload.get("pdf_error", "erro desconhecido")
            st.error(f"Falha ao gerar PDF: {err}")


def _run_pipeline(question: str) -> dict:
    """Full pipeline: generate SQL, validate, execute with 1-shot retry, summarize."""
    start = time.perf_counter()

    if not _can_call_llm():
        duration_ms = (time.perf_counter() - start) * 1000
        message = (
            f"Limite de {DEMO_QUESTION_LIMIT} perguntas por sessão atingido na demo pública. "
            "Cole sua própria chave Anthropic na barra lateral para continuar sem limites, "
            "ou recarregue a página para iniciar uma nova sessão."
        )
        return {
            "question": question,
            "sql": "",
            "ok": False,
            "error": message,
            "dataframe": None,
            "insights": "",
            "duration_ms": duration_ms,
            "rate_limited": True,
        }

    client = _get_active_client()

    upload_ctx = st.session_state.get("upload_context")
    schema = upload_ctx["schema"] if upload_ctx else None
    tbl_name = next(iter(upload_ctx["allowed_tables"])) if upload_ctx else None
    allowed = upload_ctx["allowed_tables"] if upload_ctx else None

    destructive_hit = detect_destructive_intent(question)
    if destructive_hit:
        duration_ms = (time.perf_counter() - start) * 1000
        message = (
            "Pergunta bloqueada pela camada de segurança: a solicitação contém "
            "termos associados a operações destrutivas (ex.: apagar, atualizar, remover). "
            "Este sistema aceita apenas consultas de leitura."
        )
        log_interaction(
            question=question,
            sql="",
            status="blocked_intent",
            rows=0,
            error=f"destructive intent: {destructive_hit}",
            duration_ms=duration_ms,
        )
        return {
            "question": question,
            "sql": "-- bloqueado pela camada de segurança (intenção destrutiva)",
            "ok": False,
            "error": message,
            "dataframe": None,
            "insights": "",
            "duration_ms": duration_ms,
            "blocked": True,
        }

    try:
        _record_llm_call()
        raw_sql = client.generate_sql(question, schema=schema, table_name=tbl_name)
        validation = validate_sql(raw_sql, allowed_tables=allowed)

        if not validation.is_valid:
            retry_sql = client.generate_sql(
                question, error_context=validation.error or "",
                schema=schema, table_name=tbl_name,
            )
            validation = validate_sql(retry_sql, allowed_tables=allowed)
            raw_sql = retry_sql

        result: QueryResult = run_query(raw_sql, allowed_tables=allowed)

        if not result.ok:
            retry_sql = client.generate_sql(
                question, error_context=result.error or "",
                schema=schema, table_name=tbl_name,
            )
            result = run_query(retry_sql, allowed_tables=allowed)
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        message = str(exc) or "Erro inesperado ao consultar o modelo."
        logger.warning("llm pipeline failed: %s", exc)
        log_interaction(
            question=question,
            sql="",
            status="llm_error",
            rows=0,
            error=message,
            duration_ms=duration_ms,
        )
        return {
            "question": question,
            "sql": "",
            "ok": False,
            "error": message,
            "dataframe": None,
            "insights": "",
            "duration_ms": duration_ms,
            "llm_error": True,
        }

    duration_ms = (time.perf_counter() - start) * 1000

    payload: dict = {
        "question": question,
        "sql": result.sql,
        "ok": result.ok,
        "error": result.error,
        "dataframe": result.dataframe,
        "insights": "",
        "duration_ms": duration_ms,
    }

    if result.ok and result.dataframe is not None and not result.dataframe.empty:
        try:
            payload["insights"] = generate_insights(client, question, result.sql, result.dataframe)
        except Exception as exc:
            logger.exception("insight generation failed")
            payload["insights"] = f"_Falha ao gerar insights: {exc}_"

    log_interaction(
        question=question,
        sql=result.sql,
        status="ok" if result.ok else "error",
        rows=0 if result.dataframe is None else len(result.dataframe),
        error=result.error,
        duration_ms=duration_ms,
    )
    return payload


def _get_response(question: str) -> dict:
    key = _cache_key(question)
    cache = st.session_state["response_cache"]
    if key in cache:
        cached = cache[key]
        cached["cached"] = True
        return cached
    payload = _run_pipeline(question)
    payload["cached"] = False
    if not payload.get("rate_limited"):
        cache[key] = payload
    return payload


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _handle_file_uploads() -> None:
    """Process multi-file upload. Each file becomes a separate SQLite table."""
    uploaded_files = st.file_uploader(
        "Enviar CSV ou XLSX",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        key="data_uploader",
        help="Arraste um ou mais arquivos. Cada arquivo vira uma tabela; "
        "múltiplas tabelas podem ser cruzadas via JOIN.",
    )

    if uploaded_files:
        combined_hash = sha1(
            b"".join(f.getvalue() for f in uploaded_files)
        ).hexdigest()

        ctx = st.session_state.get("upload_context")
        if ctx and ctx.get("files_hash") == combined_hash:
            _show_upload_info(ctx)
            return

        tables: dict[str, dict] = {}
        schemas: list[str] = []
        allowed: set[str] = set()

        for f in uploaded_files:
            f.seek(0)
            table_name = _sanitize_table_name(f.name)
            base = table_name
            counter = 2
            while table_name in tables:
                table_name = f"{base}_{counter}"
                counter += 1

            if f.name.lower().endswith(".csv"):
                df = pd.read_csv(f)
            else:
                df = pd.read_excel(f)

            df = _normalize_columns(df)
            load_dataframe(df, table_name)
            schemas.append(_build_schema_from_df(df, table_name))
            tables[table_name] = {"filename": f.name, "row_count": len(df)}
            allowed.add(table_name)

        st.session_state["upload_context"] = {
            "tables": tables,
            "schema": "\n\n".join(schemas),
            "allowed_tables": allowed,
            "files_hash": combined_hash,
        }
        st.session_state["response_cache"] = {}
        st.session_state["history"] = []
        st.session_state.pop("comparison_payloads", None)

        _show_upload_info(st.session_state["upload_context"])

    elif st.session_state.get("upload_context"):
        st.session_state.pop("upload_context", None)
        st.session_state["response_cache"] = {}
        st.session_state["history"] = []
        st.session_state.pop("comparison_payloads", None)


def _show_upload_info(ctx: dict) -> None:
    for tbl_name, info in ctx["tables"].items():
        st.success(f"**{info['filename']}** → `{tbl_name}` ({info['row_count']} linhas)")


def _render_sidebar(inserted_rows: int) -> None:
    with st.sidebar:
        st.subheader("⚙️ Configuração")
        st.write(f"**Provider:** `{SETTINGS.llm_provider}`")
        st.write(f"**Modelo:** `{SETTINGS.llm_model}`")
        if SETTINGS.has_llm_credentials:
            st.success("Credenciais LLM carregadas.")
        else:
            st.warning("Sem API key — rodando em modo heurístico offline.")

        st.markdown("---")
        st.subheader("🔑 Chave própria (opcional)")
        st.text_input(
            f"Sua API key {SETTINGS.llm_provider.capitalize()}",
            type="password",
            key="user_api_key",
            placeholder="sk-ant-..." if SETTINGS.llm_provider == "anthropic" else "sk-...",
            help=(
                f"Opcional. Sem ela, a demo usa a chave do mantenedor "
                f"(limite de {DEMO_QUESTION_LIMIT} perguntas/sessão). "
                "A chave fica só na sua sessão — não é salva."
            ),
        )
        remaining = max(0, DEMO_QUESTION_LIMIT - st.session_state.get("question_count", 0))
        if st.session_state.get("user_api_key", "").strip():
            st.success("Usando sua chave — sem limite.")
        elif remaining == 0:
            st.error("Limite da demo atingido. Cole sua chave para continuar.")
        elif remaining <= 5:
            st.warning(f"Demo: {remaining} perguntas restantes.")
        else:
            st.caption(f"Demo: {remaining} perguntas restantes nesta sessão.")

        upload_ctx = st.session_state.get("upload_context")

        if not upload_ctx and inserted_rows > 0:
            st.info(f"Banco populado com {inserted_rows} linhas fictícias.")

        st.markdown("---")
        st.subheader("📁 Seus dados")
        _handle_file_uploads()

        st.markdown("---")
        st.subheader("⚖️ Modo comparação")
        st.checkbox(
            "Comparar duas perguntas",
            key="comparison_mode",
            help="Quando ativo, o formulário aceita 2 perguntas e mostra os resultados lado a lado.",
        )

        st.markdown("---")
        st.subheader("🧱 Schema")
        active_schema = upload_ctx["schema"] if upload_ctx else SCHEMA_DESCRIPTION
        st.code(active_schema, language="text")

        if not upload_ctx:
            st.markdown("---")
            st.subheader("🧪 Exemplos")
            for example in EXAMPLE_QUESTIONS:
                if st.button(example, key=f"ex_{example}", width="stretch"):
                    st.session_state["pending_question"] = example
                    st.rerun()

        st.markdown("---")
        if st.button("🗑️ Limpar histórico", width="stretch"):
            st.session_state["history"] = []
            st.session_state["response_cache"] = {}
            st.session_state.pop("comparison_payloads", None)
            st.rerun()


def _render_result(payload: dict, key_prefix: str = "latest") -> None:
    if payload.get("rate_limited"):
        st.warning(f"🚦 {payload['error']}")
        return

    if payload.get("blocked"):
        st.warning(f"🛡️ {payload['error']}")
        with st.expander("Detalhes técnicos", expanded=False):
            st.caption(
                "Esta verificação é a primeira de três camadas de segurança: "
                "1) detecção de intenção na pergunta, "
                "2) validação sintática do SQL gerado pelo LLM, "
                "3) sandbox de execução apenas-leitura no SQLite."
            )
        return

    if payload.get("llm_error"):
        st.warning(f"⏳ {payload['error']}")
        st.caption(
            "Esse erro costuma ocorrer quando a API do provedor LLM está "
            "temporariamente sobrecarregada. O sistema já tentou 3 vezes com backoff. "
            "Aguarde alguns segundos e envie a pergunta de novo."
        )
        return

    st.markdown("#### 🧾 SQL gerada")
    st.code(payload["sql"], language="sql")

    explain_key = f"explain_{key_prefix}"
    if st.button("💬 Explicar essa query", key=f"btn_{explain_key}"):
        if "explanation" not in payload:
            if not _can_call_llm():
                payload["explanation"] = (
                    f"_Limite de {DEMO_QUESTION_LIMIT} perguntas por sessão atingido. "
                    "Cole sua chave na barra lateral para continuar._"
                )
            else:
                try:
                    with st.spinner("Pedindo ao LLM para explicar a query..."):
                        _record_llm_call()
                        payload["explanation"] = _get_active_client().explain_sql(payload["sql"])
                except Exception as exc:
                    payload["explanation"] = f"_Não foi possível gerar a explicação: {exc}_"
        st.session_state[explain_key] = True

    if st.session_state.get(explain_key) and payload.get("explanation"):
        st.info(payload["explanation"])

    if not payload["ok"]:
        st.error(f"Falha ao executar a consulta: {payload['error']}")
        return

    df: pd.DataFrame | None = payload["dataframe"]
    if df is None or df.empty:
        st.info("A consulta não retornou linhas.")
        return

    col_left, col_right = st.columns([1.1, 1])
    with col_left:
        st.markdown("#### 📋 Resultado")
        st.dataframe(df, width="stretch", hide_index=True)

        meta_parts = [f"{len(df)} linhas", f"{payload['duration_ms']:.0f} ms"]
        meta_line = " · ".join(meta_parts)
        if payload.get("cached"):
            st.markdown(
                f"<span style='background:#FFF3CD;color:#664D03;padding:2px 8px;"
                f"border-radius:10px;font-size:0.78rem;font-weight:600;'>⚡ cache hit</span> "
                f"<span style='color:#666;font-size:0.8rem;'>{meta_line}</span>",
                unsafe_allow_html=True,
            )
        else:
            st.caption(meta_line)

        dl_csv, dl_xlsx, dl_pdf = st.columns(3)
        with dl_csv:
            st.download_button(
                label="📥 CSV",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=f"resultado_{key_prefix}.csv",
                mime="text/csv",
                key=f"dl_csv_{key_prefix}",
                width="stretch",
            )
        with dl_xlsx:
            st.download_button(
                label="📊 XLSX",
                data=_dataframe_to_xlsx(df),
                file_name=f"resultado_{key_prefix}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"dl_xlsx_{key_prefix}",
                width="stretch",
            )
        with dl_pdf:
            _render_pdf_button(payload, key_prefix)
    with col_right:
        fig = build_chart(df)
        if fig is not None:
            st.markdown("#### 📈 Visualização")
            st.plotly_chart(fig, width="stretch", key=f"chart_{key_prefix}")
        else:
            st.markdown("#### 📈 Visualização")
            st.caption("Sem gráfico adequado para este resultado.")

    if payload["insights"]:
        st.markdown("#### 💡 Insights")
        st.markdown(payload["insights"].replace("$", "\\$"))


def _render_comparison_panel(payload: dict, key_prefix: str) -> None:
    """Compact vertical render used inside the side-by-side comparison view."""
    if payload.get("rate_limited"):
        st.warning(f"🚦 {payload['error']}")
        return
    if payload.get("blocked"):
        st.warning(f"🛡️ {payload['error']}")
        return
    if payload.get("llm_error"):
        st.warning(f"⏳ {payload['error']}")
        return

    with st.expander("🧾 SQL gerada", expanded=False):
        st.code(payload["sql"], language="sql")

    if not payload["ok"]:
        st.error(f"Falha: {payload['error']}")
        return

    df: pd.DataFrame | None = payload["dataframe"]
    if df is None or df.empty:
        st.info("A consulta não retornou linhas.")
        return

    fig = build_chart(df)
    if fig is not None:
        st.plotly_chart(fig, width="stretch", key=f"chart_{key_prefix}")

    st.dataframe(df, width="stretch", hide_index=True, height=220)

    meta_line = f"{len(df)} linhas · {payload['duration_ms']:.0f} ms"
    if payload.get("cached"):
        meta_line += " · ⚡ cache hit"
    st.caption(meta_line)

    dl_csv, dl_xlsx, dl_pdf = st.columns(3)
    with dl_csv:
        st.download_button(
            label="📥 CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"resultado_{key_prefix}.csv",
            mime="text/csv",
            key=f"dl_csv_{key_prefix}",
            width="stretch",
        )
    with dl_xlsx:
        st.download_button(
            label="📊 XLSX",
            data=_dataframe_to_xlsx(df),
            file_name=f"resultado_{key_prefix}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_xlsx_{key_prefix}",
            width="stretch",
        )
    with dl_pdf:
        _render_pdf_button(payload, key_prefix)

    if payload["insights"]:
        with st.expander("💡 Insights", expanded=True):
            st.markdown(payload["insights"].replace("$", "\\$"))


def _render_comparison(payload_a: dict, payload_b: dict) -> None:
    st.markdown("### ⚖️ Comparação lado a lado")
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(f"**🅰️ {payload_a['question']}**")
        _render_comparison_panel(payload_a, "cmp_a")
    with col_b:
        st.markdown(f"**🅱️ {payload_b['question']}**")
        _render_comparison_panel(payload_b, "cmp_b")


def _render_history() -> None:
    history = st.session_state["history"]
    if len(history) <= 1:
        return
    st.markdown("---")
    st.subheader("🕘 Histórico da sessão")
    st.caption("Clique em uma pergunta para expandir ou use 🔄 para trazê-la de volta ao topo (usa cache, instantâneo).")
    for i, item in enumerate(reversed(history[:-1]), start=1):
        with st.expander(f"{i}. {item['question']}", expanded=False):
            col_replay, col_spacer = st.columns([1, 3])
            with col_replay:
                if st.button("🔄 Executar de novo", key=f"replay_{i}", width="stretch"):
                    payload = _get_response(item["question"])
                    st.session_state["history"].append(payload)
                    st.rerun()
            _render_result(item, key_prefix=f"hist_{i}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, page_icon=PAGE_ICON, layout="wide")
    _init_session_state()
    boot = _bootstrap()

    st.title(f"{PAGE_ICON} {PAGE_TITLE}")
    upload_ctx = st.session_state.get("upload_context")
    if upload_ctx:
        total_rows = sum(t["row_count"] for t in upload_ctx["tables"].values())
        n_tables = len(upload_ctx["tables"])
        files = ", ".join(t["filename"] for t in upload_ctx["tables"].values())
        if n_tables == 1:
            st.caption(
                f"Analisando **{files}** ({total_rows} linhas). "
                "Pergunte em linguagem natural sobre qualquer aspecto dos dados."
            )
        else:
            st.caption(
                f"Analisando **{n_tables} tabelas** ({total_rows} linhas): {files}. "
                "Pergunte em linguagem natural — o sistema gera JOINs automaticamente quando necessário."
            )
    else:
        st.caption(
            "Pergunte em linguagem natural sobre os dados de vendas. "
            "O sistema gera SQL, valida segurança, executa e resume os insights."
        )

    _render_sidebar(boot["inserted"])

    if st.session_state.get("comparison_mode"):
        _render_comparison_flow()
    else:
        _render_single_flow()

    _render_history()


def _render_single_flow() -> None:
    pending = st.session_state.get("pending_question", "")
    with st.form("question_form", clear_on_submit=False):
        question = st.text_input(
            "Sua pergunta",
            value=pending,
            placeholder="Ex.: Qual o total de vendas por região no último trimestre?",
        )
        submitted = st.form_submit_button("Analisar", type="primary", width="stretch")

    if submitted and question.strip():
        st.session_state["pending_question"] = ""
        with st.spinner("Gerando SQL, executando e analisando..."):
            payload = _get_response(question.strip())
        if not payload.get("rate_limited"):
            st.session_state["history"].append(payload)
        _render_result(payload)
    elif st.session_state["history"]:
        _render_result(st.session_state["history"][-1])
    else:
        ctx = st.session_state.get("upload_context")
        if ctx:
            st.info("Digite uma pergunta sobre seus dados carregados.")
        else:
            st.info(
                "Digite uma pergunta acima ou escolha um exemplo na barra lateral. "
                f"Os dados estão na tabela `{TABLE_NAME}`."
            )


def _render_comparison_flow() -> None:
    st.caption("⚖️ Modo comparação ativo — digite duas perguntas para analisar lado a lado.")
    with st.form("comparison_form", clear_on_submit=False):
        col_a, col_b = st.columns(2)
        with col_a:
            question_a = st.text_input(
                "🅰️ Pergunta A",
                placeholder="Ex.: Qual o total de vendas por região?",
                key="comparison_q_a",
            )
        with col_b:
            question_b = st.text_input(
                "🅱️ Pergunta B",
                placeholder="Ex.: Qual o total de vendas por canal?",
                key="comparison_q_b",
            )
        submitted = st.form_submit_button("Comparar", type="primary", width="stretch")

    if submitted and question_a.strip() and question_b.strip():
        with st.spinner("Executando as duas perguntas..."):
            payload_a = _get_response(question_a.strip())
            payload_b = _get_response(question_b.strip())
        if not payload_a.get("rate_limited"):
            st.session_state["history"].append(payload_a)
        if not payload_b.get("rate_limited"):
            st.session_state["history"].append(payload_b)
        st.session_state["comparison_payloads"] = (payload_a, payload_b)
        _render_comparison(payload_a, payload_b)
    elif st.session_state.get("comparison_payloads"):
        _render_comparison(*st.session_state["comparison_payloads"])
    else:
        st.info(
            "Digite duas perguntas acima para comparar os resultados lado a lado. "
            "Ótimo para contrastar dimensões (ex.: vendas por região × vendas por canal)."
        )


if __name__ == "__main__":
    main()
