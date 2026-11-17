"""Relatório semanal de operações.

- `relatorio_deterministico()` roda a mesma investigação que o agente faria,
  mas com regras fixas e sem LLM. Funciona offline e serve de baseline.
- `PERGUNTA_RELATORIO` é o prompt usado quando há um LLM disponível: o agente
  decide sozinho o caminho da investigação e escreve o relatório.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .ferramentas import (
    analisar_avaliacoes,
    calcular_kpis,
    consultar_sql,
    decompor_tempo_entrega,
    detectar_anomalias,
)

PERGUNTA_RELATORIO = (
    "Monte o relatório semanal de operações: o que piorou na última semana em relação "
    "às semanas anteriores, qual a causa raiz de cada problema, o impacto em pedidos e "
    "faturamento e quais ações cada time deve tomar, em ordem de prioridade."
)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _restaurantes_da_regiao(regiao: str) -> set[str]:
    from .ferramentas import _df

    df = _df()
    return set(df.loc[df["regiao"] == regiao, "restaurante"].unique())


def investigar() -> dict[str, Any]:
    """Executa a investigação padrão e devolve os achados estruturados."""
    semana = calcular_kpis()["kpis"]
    fim = date.fromisoformat(calcular_kpis()["filtros"]["data_fim"])
    anterior = calcular_kpis(
        data_inicio=str(fim - timedelta(days=34)), data_fim=str(fim - timedelta(days=7))
    )["kpis"]

    dias = detectar_anomalias("taxa_no_prazo", "data", z_min=4)["anomalias"]
    atipicos = [d["entidade"] for d in dias if d["z_score"] <= -10]

    achados: list[dict[str, Any]] = []
    for d in dias:
        if d["entidade"] in atipicos:
            achados.append({
                "tipo": "evento externo", "alvo": f"Dia {d['entidade']}",
                "evidencia": f"taxa no prazo {_pct(d['valor_dia'])} vs. média de {_pct(d['linha_de_base'])}",
                "causa": "queda em todas as regiões no mesmo dia (provável chuva forte/evento externo)",
                "acao": "Ativar plano de contingência para chuva (incentivo dinâmico a entregadores, ampliar ETA exibido ao cliente).",
                "prioridade": "média", "dono": "Logística",
            })

    regioes_hora = detectar_anomalias("taxa_no_prazo", "regiao_faixa_horaria", excluir_datas=atipicos, z_min=8)["anomalias"]
    regioes_com_problema: set[str] = set()
    for a in regioes_hora:
        regiao, faixa = a["entidade"].split(" · ")
        h_ini, h_fim = (19, 22) if "jantar" in faixa else (11, 14) if "almoço" in faixa else (15, 18) if "tarde" in faixa else (None, None)
        dec = decompor_tempo_entrega(regiao=regiao, hora_inicio=h_ini, hora_fim=h_fim)
        if dec.get("principal_causa") != "espera por entregador":
            continue  # problema de restaurante: será tratado abaixo
        regioes_com_problema.add(regiao)
        etapa = next(e for e in dec["etapas"] if e["etapa"] == "espera por entregador")
        achados.append({
            "tipo": "logística", "alvo": f"{regiao}, {faixa}",
            "evidencia": f"taxa no prazo {_pct(a['linha_de_base'])} → {_pct(a['janela_recente'])}; espera por entregador {etapa['linha_de_base_min']} → {etapa['janela_recente_min']} min",
            "causa": "falta de entregadores no pico (o preparo e o trajeto não mudaram)",
            "acao": f"Reforçar a oferta de entregadores em {regiao} no {faixa} (incentivo de pico, remanejar de regiões vizinhas).",
            "prioridade": "alta", "dono": "Logística",
        })

    rest_anom = detectar_anomalias("taxa_no_prazo", "restaurante", excluir_datas=atipicos)["anomalias"]
    afetados_regiao = set().union(*(_restaurantes_da_regiao(r) for r in regioes_com_problema)) if regioes_com_problema else set()
    for a in rest_anom:
        if a["entidade"] in afetados_regiao:
            continue  # explicado pelo problema da região
        dec = decompor_tempo_entrega(restaurante=a["entidade"])
        if dec.get("principal_causa") != "preparo no restaurante":
            continue
        prep = next(e for e in dec["etapas"] if e["etapa"] == "preparo no restaurante")
        av = analisar_avaliacoes(restaurante=a["entidade"])
        temas = ", ".join(list(av["recente"]["temas_negativos"])[:2]) or "—"
        achados.append({
            "tipo": "restaurante", "alvo": a["entidade"],
            "evidencia": f"taxa no prazo {_pct(a['linha_de_base'])} → {_pct(a['janela_recente'])}; preparo {prep['linha_de_base_min']} → {prep['janela_recente_min']} min; avaliações negativas {_pct(av['linha_de_base']['pct_negativas'])} → {_pct(av['recente']['pct_negativas'])} (temas: {temas})",
            "causa": "tempo de preparo disparou (cozinha sobrecarregada ou desfalque de equipe)",
            "acao": "Contato do key account, ajustar o tempo de preparo informado e limitar o volume no pico até normalizar.",
            "prioridade": "alta", "dono": "Gestão de Restaurantes",
        })

    for a in detectar_anomalias("taxa_cancelamento", "restaurante", excluir_datas=atipicos)["anomalias"]:
        k = calcular_kpis(restaurante=a["entidade"])["kpis"]
        fim_txt = calcular_kpis()["filtros"]  # período da última semana
        nome_sql = a["entidade"].replace("'", "''")
        motivos = consultar_sql(
            "SELECT motivo_cancelamento AS motivo, COUNT(*) AS n FROM pedidos "
            f"WHERE status='cancelado' AND restaurante='{nome_sql}' "
            f"AND data BETWEEN '{fim_txt['data_inicio']}' AND '{fim_txt['data_fim']}' "
            "GROUP BY motivo ORDER BY n DESC"
        )["resultado"]
        top = motivos[0] if motivos else {"motivo": "?", "n": 0}
        total = sum(m["n"] for m in motivos) or 1
        if top["motivo"] != "item indisponível":
            continue
        achados.append({
            "tipo": "restaurante", "alvo": a["entidade"],
            "evidencia": f"cancelamento {_pct(a['linha_de_base'])} → {_pct(a['janela_recente'])} ({k['pedidos']} pedidos na semana; {top['n'] / total:.0%} dos cancelamentos por '{top['motivo']}')",
            "causa": "ruptura de estoque/cardápio desatualizado",
            "acao": "Pedir a atualização do cardápio/estoque no portal e ativar a pausa automática de itens esgotados.",
            "prioridade": "alta", "dono": "Gestão de Restaurantes",
        })

    return {"semana": semana, "anterior": anterior, "achados": achados, "dias_atipicos": atipicos}


def relatorio_deterministico() -> str:
    inv = investigar()
    s, a = inv["semana"], inv["anterior"]
    linhas = [
        "## Relatório semanal de operações (modo sem IA)",
        "",
        "| KPI | 4 semanas anteriores | Última semana |",
        "|---|---|---|",
        f"| Taxa no prazo | {_pct(a['taxa_no_prazo'])} | {_pct(s['taxa_no_prazo'])} |",
        f"| Tempo médio de entrega | {a['tempo_medio_min']} min | {s['tempo_medio_min']} min |",
        f"| Cancelamento | {_pct(a['taxa_cancelamento'])} | {_pct(s['taxa_cancelamento'])} |",
        f"| Nota média | {a['nota_media']} | {s['nota_media']} |",
        "",
        "### Problemas encontrados (por prioridade)",
        "",
    ]
    ordem = {"alta": 0, "média": 1, "baixa": 2}
    for i, f in enumerate(sorted(inv["achados"], key=lambda x: ordem[x["prioridade"]]), 1):
        linhas += [
            f"#### {i}. {f['alvo']} ({f['tipo']}, prioridade {f['prioridade']})",
            f"- **Evidência:** {f['evidencia']}",
            f"- **Causa provável:** {f['causa']}",
            f"- **Ação ({f['dono']}):** {f['acao']}",
            "",
        ]
    if not inv["achados"]:
        linhas.append("Nenhuma piora relevante encontrada.")
    return "\n".join(linhas)


if __name__ == "__main__":
    print(relatorio_deterministico())
