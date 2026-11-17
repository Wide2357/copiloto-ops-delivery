"""Teste rápido do agente com um provedor real.

Uso:
    python -m copiloto.testar --provedor gemini --chave SUA_CHAVE
    python -m copiloto.testar --provedor anthropic          # usa ANTHROPIC_API_KEY
    python -m copiloto.testar --provedor gemini --pergunta "Por que Tatuapé atrasou?"

Mostra cada ferramenta chamada, a resposta final e, se der erro, o que fazer.
"""

from __future__ import annotations

import argparse
import json
import sys

from .agente import PROVEDORES, Agente, criar_backend

PERGUNTA_PADRAO = "O que piorou na última semana e onde devo agir primeiro?"

DICAS = {
    "401": "Chave de API inválida. Gere outra e confira se copiou inteira.",
    "403": "A chave não tem permissão para esse modelo/projeto.",
    "404": "Modelo não encontrado. Troque com --modelo (veja a lista de modelos na documentação do provedor).",
    "429": "Limite de uso atingido (comum na camada gratuita). Espere um minuto e tente de novo.",
    "503": "Servidor do provedor sobrecarregado. Tente de novo em 1-2 minutos ou use outro modelo, ex.: --modelo gemini-2.5-flash",
    "500": "Erro temporário no servidor do provedor. Tente de novo em instantes.",
    "400": "O provedor recusou a requisição. Copie a mensagem de erro acima e me mande.",
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Teste rápido do Copiloto com um LLM real.")
    ap.add_argument("--provedor", default="gemini", choices=list(PROVEDORES))
    ap.add_argument("--modelo", default=None)
    ap.add_argument("--chave", default=None, help="Se omitida, usa a variável de ambiente do provedor.")
    ap.add_argument("--pergunta", default=PERGUNTA_PADRAO)
    args = ap.parse_args()

    try:
        agente = Agente(criar_backend(args.provedor, args.chave, args.modelo))
    except ValueError as e:
        sys.exit(f"❌ {e}")

    print(f"Provedor: {args.provedor} · modelo: {agente.backend.modelo}")
    print(f"Pergunta: {args.pergunta}\n")

    def mostrar(passo):
        print(f"🔧 {passo.ferramenta}({json.dumps(passo.argumentos, ensure_ascii=False)})  [{passo.duracao_ms} ms]")

    try:
        r = agente.perguntar(args.pergunta, ao_passo=mostrar)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        print(f"\n❌ Erro do provedor: {type(e).__name__}: {msg[:800]}")
        for codigo, dica in DICAS.items():
            if codigo in msg:
                print(f"👉 {dica}")
                break
        sys.exit(1)

    print("\n" + "─" * 70)
    print(r.texto)
    print("─" * 70)
    print(f"✅ {len(r.passos)} ferramentas em {r.duracao_s}s" + (" (atingiu o limite de passos)" if r.atingiu_limite else ""))
    if not r.passos:
        print("⚠️  O agente respondeu sem consultar nenhuma ferramenta: os números podem ser inventados. Tente outro modelo.")


if __name__ == "__main__":
    main()
