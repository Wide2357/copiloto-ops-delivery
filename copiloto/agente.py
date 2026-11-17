"""Loop do agente com tool calling, independente de provedor.

Provedores suportados:
- **anthropic**  → Claude (API nativa da Anthropic)
- **openai**     → GPT (API da OpenAI)
- **gemini**     → Gemini, via endpoint compatível com OpenAI (tem camada gratuita)
- qualquer outro endpoint compatível com OpenAI (Groq, OpenRouter, Ollama...)
  via OPENAI_BASE_URL.

O loop é sempre o mesmo: o modelo pede ferramentas → executamos em Python →
devolvemos o resultado → repete até o modelo responder em texto (ou atingir o
limite de passos). Cada passo fica registrado para auditoria na interface.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .ferramentas import FERRAMENTAS, _df, _hoje, executar_ferramenta, ferramentas_formato_openai
from .prompts import SYSTEM_PROMPT

MAX_PASSOS = 10
MAX_CHARS_RESULTADO = 12_000

PROVEDORES: dict[str, dict[str, str | None]] = {
    "anthropic": {"modelo": "claude-sonnet-5-5", "env_chave": "ANTHROPIC_API_KEY", "base_url": None},
    "openai": {"modelo": "gpt-4.1-mini", "env_chave": "OPENAI_API_KEY", "base_url": None},
    "gemini": {
        "modelo": "gemini-flash-lite-latest",  # apelido do Flash-Lite mais recente (mais disponível na camada gratuita)
        "env_chave": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
}


@dataclass
class ChamadaFerramenta:
    id: str
    nome: str
    argumentos: dict[str, Any]


@dataclass
class Passo:
    ferramenta: str
    argumentos: dict[str, Any]
    resultado: str
    duracao_ms: int


@dataclass
class RespostaAgente:
    texto: str
    passos: list[Passo] = field(default_factory=list)
    provedor: str = ""
    modelo: str = ""
    duracao_s: float = 0.0
    atingiu_limite: bool = False


class Backend(Protocol):
    provedor: str
    modelo: str

    def preparar(self, system: str, historico: list[dict[str, str]], pergunta: str) -> list[Any]: ...

    def chamar(self, mensagens: list[Any]) -> tuple[str | None, list[ChamadaFerramenta], Any]: ...

    def anexar(self, mensagens: list[Any], msg_assistente: Any, resultados: list[tuple[ChamadaFerramenta, str]]) -> None: ...


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------
class BackendAnthropic:
    provedor = "anthropic"

    def __init__(self, api_key: str, modelo: str | None = None):
        import anthropic

        self.cliente = anthropic.Anthropic(api_key=api_key, max_retries=5)  # retenta 429/5xx com espera crescente
        self.modelo = modelo or PROVEDORES["anthropic"]["modelo"]
        self._system = ""

    def preparar(self, system, historico, pergunta):
        self._system = system
        return [*({"role": m["role"], "content": m["content"]} for m in historico), {"role": "user", "content": pergunta}]

    def chamar(self, mensagens):
        resp = self.cliente.messages.create(
            model=self.modelo, max_tokens=2048, system=self._system, messages=mensagens, tools=FERRAMENTAS,
        )
        texto = "".join(b.text for b in resp.content if b.type == "text").strip() or None
        chamadas = [ChamadaFerramenta(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        return texto, chamadas, resp.content

    def anexar(self, mensagens, msg_assistente, resultados):
        mensagens.append({"role": "assistant", "content": msg_assistente})
        mensagens.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": c.id, "content": r} for c, r in resultados],
        })


# ---------------------------------------------------------------------------
# OpenAI e compatíveis (GPT, Gemini, Groq, OpenRouter, Ollama...)
# ---------------------------------------------------------------------------
class BackendOpenAICompat:
    def __init__(self, api_key: str, modelo: str | None = None, base_url: str | None = None, provedor: str = "openai"):
        from openai import OpenAI

        cfg = PROVEDORES.get(provedor, PROVEDORES["openai"])
        self.provedor = provedor
        self.modelo = modelo or cfg["modelo"]
        self.cliente = OpenAI(api_key=api_key, base_url=base_url or cfg["base_url"], max_retries=5)  # retenta 429/5xx com espera crescente

    def preparar(self, system, historico, pergunta):
        return [{"role": "system", "content": system}, *historico, {"role": "user", "content": pergunta}]

    def chamar(self, mensagens):
        resp = self.cliente.chat.completions.create(
            model=self.modelo, messages=mensagens, tools=ferramentas_formato_openai(), tool_choice="auto",
        )
        msg = resp.choices[0].message
        chamadas = []
        for i, tc in enumerate(msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            chamadas.append(ChamadaFerramenta(tc.id or f"call_{i}", tc.function.name, args))
        return (msg.content or "").strip() or None, chamadas, msg

    def anexar(self, mensagens, msg_assistente, resultados):
        # Reenvia a mensagem do modelo exatamente como veio: alguns provedores
        # (ex.: Gemini 3) anexam campos extras às tool_calls, como a "thought
        # signature", e recusam a conversa se eles forem descartados.
        if hasattr(msg_assistente, "model_dump") and all(c.id for c, _ in resultados) and msg_assistente.tool_calls and all(tc.id for tc in msg_assistente.tool_calls):
            mensagens.append(msg_assistente.model_dump(exclude_none=True))
            for c, r in resultados:
                mensagens.append({"role": "tool", "tool_call_id": c.id, "content": r})
            return
        mensagens.append({
            "role": "assistant",
            "content": msg_assistente.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.nome, "arguments": json.dumps(c.argumentos, ensure_ascii=False)}}
                for c, _ in resultados
            ],
        })
        for c, r in resultados:
            mensagens.append({"role": "tool", "tool_call_id": c.id, "content": r})


def criar_backend(provedor: str, api_key: str | None = None, modelo: str | None = None, base_url: str | None = None) -> Backend:
    provedor = provedor.lower()
    cfg = PROVEDORES.get(provedor, PROVEDORES["openai"])
    chave = api_key or os.getenv(cfg["env_chave"] or "") or ""
    if not chave:
        raise ValueError(f"Defina a chave de API do provedor '{provedor}' (variável {cfg['env_chave']}).")
    if provedor == "anthropic":
        return BackendAnthropic(chave, modelo)
    return BackendOpenAICompat(chave, modelo, base_url or os.getenv("OPENAI_BASE_URL"), provedor)


# ---------------------------------------------------------------------------
# O agente
# ---------------------------------------------------------------------------
def system_prompt() -> str:
    df = _df()
    return SYSTEM_PROMPT.format(inicio=min(df["data"]), fim=_hoje(df))


class Agente:
    def __init__(self, backend: Backend, max_passos: int = MAX_PASSOS):
        self.backend = backend
        self.max_passos = max_passos

    def perguntar(
        self,
        pergunta: str,
        historico: list[dict[str, str]] | None = None,
        ao_passo: Callable[[Passo], None] | None = None,
    ) -> RespostaAgente:
        inicio = time.time()
        mensagens = self.backend.preparar(system_prompt(), historico or [], pergunta)
        passos: list[Passo] = []

        for _ in range(self.max_passos):
            texto, chamadas, msg = self.backend.chamar(mensagens)
            if not chamadas:
                return self._resposta(texto or "(sem resposta)", passos, inicio)

            resultados = []
            for c in chamadas:
                t0 = time.time()
                resultado = executar_ferramenta(c.nome, c.argumentos)[:MAX_CHARS_RESULTADO]
                passo = Passo(c.nome, c.argumentos, resultado, int((time.time() - t0) * 1000))
                passos.append(passo)
                if ao_passo:
                    ao_passo(passo)
                resultados.append((c, resultado))
            self.backend.anexar(mensagens, msg, resultados)

        # limite atingido: pede uma resposta final sem novas ferramentas
        mensagens_final = mensagens + [{"role": "user", "content": "Limite de investigação atingido. Responda agora com o que já apurou."}]
        texto, _, _ = self.backend.chamar(mensagens_final)
        r = self._resposta(texto or "Não consegui concluir a análise dentro do limite de passos.", passos, inicio)
        r.atingiu_limite = True
        return r

    def _resposta(self, texto: str, passos: list[Passo], inicio: float) -> RespostaAgente:
        return RespostaAgente(
            texto=texto, passos=passos, provedor=self.backend.provedor,
            modelo=self.backend.modelo, duracao_s=round(time.time() - inicio, 1),
        )
