"""Avaliação automática do agente.

Roda cada pergunta de `casos.json` e verifica:
  1. se a resposta menciona as conclusões esperadas (cada grupo = "pelo menos um destes termos");
  2. se não menciona conclusões erradas;
  3. se o agente usou pelo menos uma das ferramentas esperadas (ou seja, foi buscar o dado
     em vez de inventar).

Uso:
    python -m evals.rodar_evals --provedor gemini
    python -m evals.rodar_evals --provedor anthropic --modelo claude-sonnet-5-5
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from copiloto.agente import Agente, criar_backend
from copiloto.verificador import verificar

PASTA = Path(__file__).parent


def _contem(texto: str, grupo: list[str]) -> bool:
    t = texto.lower()
    return any(termo.lower() in t for termo in grupo)


def avaliar(caso: dict, resposta) -> dict:
    falhas = []
    for grupo in caso.get("deve_conter", []):
        if not _contem(resposta.texto, grupo):
            falhas.append(f"faltou mencionar: {grupo}")
    for grupo in caso.get("nao_deve_conter", []):
        if _contem(resposta.texto, grupo):
            falhas.append(f"mencionou algo errado: {grupo}")
    usadas = {p.ferramenta for p in resposta.passos}
    esperadas = set(caso.get("ferramentas_esperadas", []))
    if esperadas and not usadas & esperadas:
        falhas.append(f"não usou nenhuma de {sorted(esperadas)} (usou {sorted(usadas)})")
    if any(p.ferramenta == "consultar_sql" and "erro" not in p.resultado and "delete" in p.argumentos.get("sql", "").lower() for p in resposta.passos):
        falhas.append("executou comando destrutivo")
    v = verificar(resposta.texto, [p.resultado for p in resposta.passos])
    return {
        "passou": not falhas, "falhas": falhas, "ferramentas": [p.ferramenta for p in resposta.passos],
        "numeros_fundamentados": f"{len(v.conferidos)}/{v.total}",
        "numeros_nao_encontrados": [n.texto for n in v.nao_encontrados],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provedor", default="anthropic")
    ap.add_argument("--modelo", default=None)
    args = ap.parse_args()

    agente = Agente(criar_backend(args.provedor, modelo=args.modelo))
    casos = json.loads((PASTA / "casos.json").read_text(encoding="utf-8"))
    resultados = []
    for caso in casos:
        t0 = time.time()
        resp = agente.perguntar(caso["pergunta"])
        r = {"id": caso["id"], **avaliar(caso, resp), "segundos": round(time.time() - t0, 1)}
        resultados.append(r)
        print(f"{'✅' if r['passou'] else '❌'} {caso['id']:<18} {r['segundos']:>5}s  ferramentas={len(r['ferramentas'])}  números ok={r['numeros_fundamentados']}  {'; '.join(r['falhas'])}")

    taxa = sum(r["passou"] for r in resultados) / len(resultados)
    ok = sum(int(r["numeros_fundamentados"].split("/")[0]) for r in resultados)
    tot = sum(int(r["numeros_fundamentados"].split("/")[1]) for r in resultados)
    if tot:
        print(f"Números fundamentados nos dados: {ok}/{tot} ({ok / tot:.0%})")
    print(f"\nAcerto: {taxa:.0%} ({sum(r['passou'] for r in resultados)}/{len(resultados)}) · modelo {agente.backend.modelo}")
    saida = PASTA / "resultados" / f"{args.provedor}_{agente.backend.modelo}.json"
    saida.parent.mkdir(exist_ok=True)
    saida.write_text(json.dumps({"modelo": agente.backend.modelo, "acerto": taxa, "casos": resultados}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Resultados salvos em {saida}")


if __name__ == "__main__":
    main()
