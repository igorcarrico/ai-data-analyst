"""AI Data Analyst - Streamlit UI.

Orchestrates: NL question -> LLM SQL -> validator -> executor -> table + chart + insights.
"""
from __future__ import annotations

import time
from hashlib import sha1

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
        st.caption(
            f"{len(df)} linhas · {payload['duration_ms']:.0f} ms"
            + (" · cache" if payload.get("cached") else "")
        )
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


def _render_history() -> None:
    history = st.session_state["history"]
    if len(history) <= 1:
        return
    st.markdown("---")
    st.subheader("🕘 Histórico da sessão")
    for i, item in enumerate(reversed(history[:-1]), start=1):
        with st.expander(f"{i}. {item['question']}", expanded=False):
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

    _render_history()


if __name__ == "__main__":
    main()
