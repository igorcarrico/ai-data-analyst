"""LLM client for NL->SQL generation and insight summarization.

Supports OpenAI and Anthropic via LLM_PROVIDER env var.
Falls back to a deterministic rule-based generator when no API key is configured,
so the app stays usable for demos and tests.
"""
from __future__ import annotations

import re
from typing import Protocol

from .config import SETTINGS, SCHEMA_DESCRIPTION, TABLE_NAME

def _build_sql_system_prompt(
    schema: str = SCHEMA_DESCRIPTION, table_name: str = TABLE_NAME,
) -> str:
    return f"""Você é um gerador de SQL para SQLite.
Sua única saída deve ser uma query SQL válida. Nunca escreva texto fora da query.

Regras obrigatórias:
- Retorne SOMENTE a query SQL, sem markdown, sem comentários, sem explicações.
- Use APENAS as tabelas e colunas listadas no schema abaixo.
- Apenas SELECT. Nunca use DELETE, DROP, UPDATE, INSERT, ALTER, TRUNCATE, CREATE, REPLACE.
- Prefira agregações claras (SUM, AVG, COUNT) e GROUP BY quando fizer sentido.
- Use date(data) ou strftime('%Y-%m', data) para manipular datas no SQLite.
- Nunca invente colunas. Se não souber responder, retorne uma SELECT simples sobre a primeira tabela.
- Se houver múltiplas tabelas no schema, use JOIN para relacioná-las quando a pergunta exigir.
  Infira a chave de junção pelos nomes, tipos e valores de exemplo das colunas.

Regras de LIMIT:
- Se o usuário pedir "o maior", "o primeiro", "o único", "top 1", use LIMIT 1.
- Se o usuário pedir "top N" ou "N maiores", use LIMIT N.
- Para perguntas abertas como "quem vendeu mais", "o que vende melhor", "principais", "melhores",
  use LIMIT 5 ou LIMIT 10 para gerar um resultado exploratório (evitando LIMIT 1,
  que produz visualizações pobres com uma única barra).

Regras de ambiguidade:
- Para perguntas vagas como "quem vendeu mais", escolha a dimensão mais natural
  (geralmente produto) e inclua os top 5. A interpretação será sinalizada na camada de insights.

Schema disponível:
{schema}
"""

SQL_SYSTEM_PROMPT = _build_sql_system_prompt()

INSIGHT_SYSTEM_PROMPT = """Você é um analista de dados sênior.
Receberá a pergunta do usuário, a query SQL executada e o resultado.

Regras de resposta:
- Escreva um resumo curto e objetivo (3 a 5 bullets) com os principais insights.
- Responda no mesmo idioma da pergunta.
- Não invente números que não estejam nos dados.
- Se a pergunta do usuário for ambígua (ex.: "quem vendeu mais" sem dizer por qual dimensão),
  comece com um bullet curto iniciado com "ℹ️ Interpretação:" explicando qual dimensão você
  assumiu (ex.: produto, região, canal) e sugerindo alternativas em uma linha.
- Use R$ ao formatar valores em reais. Evite markdown LaTeX.
"""

EXPLAIN_SYSTEM_PROMPT = """Você é um professor de SQL paciente.
Receberá uma query SQL e deve explicá-la em linguagem natural, para uma pessoa de negócio.

Regras:
- Responda em português, exceto se a query usar termos visivelmente em inglês.
- Use 2 a 4 frases curtas, sem jargão técnico desnecessário.
- Explique o QUE a query faz, não COMO o SQL funciona internamente.
- Se houver agregações (SUM, AVG, COUNT), traduza para "soma", "média", "contagem".
- Se houver GROUP BY, diga "agrupado por X".
- Se houver ORDER BY ... DESC LIMIT N, diga "os N maiores".
- Não inclua a query no output. Não use markdown nem code fences.
"""


class LLMClient(Protocol):
    def generate_sql(
        self,
        question: str,
        error_context: str | None = None,
        schema: str | None = None,
        table_name: str | None = None,
    ) -> str: ...
    def generate_insights(self, question: str, sql: str, sample_markdown: str) -> str: ...
    def explain_sql(self, sql: str) -> str: ...


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class OpenAIClient:
    def __init__(self, model: str, api_key: str) -> None:
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def _chat(self, system: str, user: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
        )
        return (resp.choices[0].message.content or "").strip()

    def generate_sql(
        self,
        question: str,
        error_context: str | None = None,
        schema: str | None = None,
        table_name: str | None = None,
    ) -> str:
        sys_prompt = _build_sql_system_prompt(schema, table_name) if schema else SQL_SYSTEM_PROMPT
        user = f"Pergunta: {question}"
        if error_context:
            user += (
                f"\n\nA query anterior falhou com o erro:\n{error_context}\n"
                "Gere uma nova query SQL corrigida, seguindo todas as regras."
            )
        return _strip_sql(self._chat(sys_prompt, user))

    def generate_insights(self, question: str, sql: str, sample_markdown: str) -> str:
        user = (
            f"Pergunta: {question}\n\n"
            f"SQL executada:\n{sql}\n\n"
            f"Resultado (primeiras linhas):\n{sample_markdown}"
        )
        return self._chat(INSIGHT_SYSTEM_PROMPT, user)

    def explain_sql(self, sql: str) -> str:
        return self._chat(EXPLAIN_SYSTEM_PROMPT, f"Query SQL:\n{sql}")


class AnthropicClient:
    def __init__(self, model: str, api_key: str) -> None:
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def _chat(self, system: str, user: str) -> str:
        import time
        from anthropic import APIStatusError, APITimeoutError, RateLimitError
        try:
            from anthropic import OverloadedError
        except ImportError:
            OverloadedError = APIStatusError

        last_exc: Exception | None = None
        for attempt, delay in enumerate([0, 1.5, 4.0]):
            if delay:
                time.sleep(delay)
            try:
                msg = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    temperature=0.1,
                )
                parts = [
                    block.text for block in msg.content
                    if getattr(block, "type", "") == "text"
                ]
                return "".join(parts).strip()
            except (OverloadedError, RateLimitError, APITimeoutError) as exc:
                last_exc = exc
                continue
            except APIStatusError as exc:
                if getattr(exc, "status_code", None) in (429, 502, 503, 529):
                    last_exc = exc
                    continue
                raise

        raise RuntimeError(
            "A API da Anthropic está temporariamente sobrecarregada. "
            "Tente novamente em alguns instantes."
        ) from last_exc

    def generate_sql(
        self,
        question: str,
        error_context: str | None = None,
        schema: str | None = None,
        table_name: str | None = None,
    ) -> str:
        sys_prompt = _build_sql_system_prompt(schema, table_name) if schema else SQL_SYSTEM_PROMPT
        user = f"Pergunta: {question}"
        if error_context:
            user += (
                f"\n\nA query anterior falhou com o erro:\n{error_context}\n"
                "Gere uma nova query SQL corrigida, seguindo todas as regras."
            )
        return _strip_sql(self._chat(sys_prompt, user))

    def generate_insights(self, question: str, sql: str, sample_markdown: str) -> str:
        user = (
            f"Pergunta: {question}\n\n"
            f"SQL executada:\n{sql}\n\n"
            f"Resultado (primeiras linhas):\n{sample_markdown}"
        )
        return self._chat(INSIGHT_SYSTEM_PROMPT, user)

    def explain_sql(self, sql: str) -> str:
        return self._chat(EXPLAIN_SYSTEM_PROMPT, f"Query SQL:\n{sql}")


class HeuristicClient:
    """Offline fallback when no API key is available. Covers common demo questions."""

    def generate_sql(
        self,
        question: str,
        error_context: str | None = None,
        schema: str | None = None,
        table_name: str | None = None,
    ) -> str:
        tbl = table_name or TABLE_NAME
        if tbl != TABLE_NAME:
            return f"SELECT * FROM {tbl} LIMIT 20"
        q = question.lower()
        if "região" in q or "regiao" in q:
            return (
                "SELECT regiao, SUM(valor) AS total_vendas, SUM(quantidade) AS total_qtd "
                f"FROM {tbl} GROUP BY regiao ORDER BY total_vendas DESC"
            )
        if "categoria" in q:
            return (
                "SELECT categoria, SUM(valor) AS total_vendas "
                f"FROM {tbl} GROUP BY categoria ORDER BY total_vendas DESC"
            )
        if "mês" in q or "mes" in q or "mensal" in q or "evolução" in q or "evolucao" in q:
            return (
                "SELECT strftime('%Y-%m', data) AS mes, SUM(valor) AS total_vendas "
                f"FROM {tbl} GROUP BY mes ORDER BY mes"
            )
        if "canal" in q:
            return (
                "SELECT canal, SUM(valor) AS total_vendas "
                f"FROM {tbl} GROUP BY canal ORDER BY total_vendas DESC"
            )
        if "produto" in q or "top" in q or "mais vendidos" in q:
            return (
                "SELECT produto, SUM(valor) AS total_vendas, SUM(quantidade) AS qtd "
                f"FROM {tbl} GROUP BY produto ORDER BY total_vendas DESC LIMIT 10"
            )
        return f"SELECT * FROM {tbl} ORDER BY data DESC LIMIT 20"

    def generate_insights(self, question: str, sql: str, sample_markdown: str) -> str:
        return (
            "- Modo offline: nenhuma chave de API configurada.\n"
            "- Os dados acima representam a resposta direta da query.\n"
            "- Configure OPENAI_API_KEY ou ANTHROPIC_API_KEY no `.env` para insights automáticos."
        )

    def explain_sql(self, sql: str) -> str:
        return (
            "Modo offline: explicação não disponível sem LLM real. "
            "Configure uma API key para ativar este recurso."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _strip_sql(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip().rstrip(";").strip()


def build_client() -> LLMClient:
    if not SETTINGS.has_llm_credentials:
        return HeuristicClient()
    if SETTINGS.llm_provider == "openai":
        return OpenAIClient(SETTINGS.llm_model, SETTINGS.openai_api_key or "")
    if SETTINGS.llm_provider == "anthropic":
        return AnthropicClient(SETTINGS.llm_model, SETTINGS.anthropic_api_key or "")
    return HeuristicClient()
