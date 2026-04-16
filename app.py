"""AI Data Analyst - Streamlit UI.

Orchestrates: NL question -> LLM SQL -> validator -> executor -> table + chart + insights.
"""
from __future__ import annotations

import time
from datetime import datetime
from hashlib import sha1
from io import BytesIO

import pandas as pd
import streamlit as st

from src.charts import build_chart
from src.config import SCHEMA_DESCRIPTION, SETTINGS, TABLE_NAME
from src.insights import generate_insights
from src.llm import build_client
from src.logger import log_interaction, logger
from src.query_executor import QueryResult, run_query
from src.sample_data import seed_if_empty
from src.sql_validator import detect_destructive_intent, validate_sql

PAGE_TITLE = "AI Data Analyst"
PAGE_ICON = "📊"

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


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def _cache_key(question: str) -> str:
    return sha1(question.strip().lower().encode("utf-8")).hexdigest()


def _dataframe_to_xlsx(df: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Resultado")
    return buffer.getvalue()


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
            fig = build_chart(df)
            if fig is not None:
                img_bytes = fig.to_image(format="png", width=900, height=500, scale=2)
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
    client = _bootstrap()["client"]
    start = time.perf_counter()

    destructive_hit = detect_destructive_intent(question)
    if destructive_hit:
        duration_ms = (time.perf_counter() - start) * 1000
        message = (
            "Pergunta bloqueada pela camada de segurança: a solicitação contém "
            "termos associados a operações destrutivas (ex.: apagar, atualizar, remover). "
            "Este sistema aceita apenas consultas de leitura sobre a tabela `vendas`."
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
        raw_sql = client.generate_sql(question)
        validation = validate_sql(raw_sql)

        if not validation.is_valid:
            retry_sql = client.generate_sql(question, error_context=validation.error or "")
            validation = validate_sql(retry_sql)
            raw_sql = retry_sql

        result: QueryResult = run_query(raw_sql)

        if not result.ok:
            retry_sql = client.generate_sql(question, error_context=result.error or "")
            result = run_query(retry_sql)
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
    cache[key] = payload
    return payload


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _render_sidebar(inserted_rows: int) -> None:
    with st.sidebar:
        st.subheader("⚙️ Configuração")
        st.write(f"**Provider:** `{SETTINGS.llm_provider}`")
        st.write(f"**Modelo:** `{SETTINGS.llm_model}`")
        if SETTINGS.has_llm_credentials:
            st.success("Credenciais LLM carregadas.")
        else:
            st.warning("Sem API key — rodando em modo heurístico offline.")

        if inserted_rows > 0:
            st.info(f"Banco populado com {inserted_rows} linhas fictícias.")

        st.markdown("---")
        st.subheader("⚖️ Modo comparação")
        st.checkbox(
            "Comparar duas perguntas",
            key="comparison_mode",
            help="Quando ativo, o formulário aceita 2 perguntas e mostra os resultados lado a lado.",
        )

        st.markdown("---")
        st.subheader("🧱 Schema")
        st.code(SCHEMA_DESCRIPTION, language="text")

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
            try:
                with st.spinner("Pedindo ao LLM para explicar a query..."):
                    payload["explanation"] = _bootstrap()["client"].explain_sql(payload["sql"])
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
        st.session_state["history"].append(payload)
        _render_result(payload)
    elif st.session_state["history"]:
        _render_result(st.session_state["history"][-1])
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
        st.session_state["history"].append(payload_a)
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
