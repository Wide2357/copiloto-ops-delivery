"""Ferramentas (tools) que o agente pode chamar.

Cada ferramenta é uma função Python determinística que devolve um dicionário
pequeno e serializável. O LLM nunca "inventa" números: ele decide QUAL
ferramenta chamar e COMO interpretar o resultado.

As datas são sempre relativas ao último dia presente nos dados (não ao relógio
do computador), para que a análise seja reprodutível.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .dados import CAMINHO_DB, carregar_pedidos, garantir_banco

LIMITE_LINHAS_SQL = 200


# ---------------------------------------------------------------------------
# Acesso aos dados
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4)
def _df(caminho: str = str(CAMINHO_DB)) -> pd.DataFrame:
    df = carregar_pedidos(Path(caminho))
    df["data"] = pd.to_datetime(df["data"]).dt.date
    return df


def _hoje(df: pd.DataFrame) -> date:
    return max(df["data"])


def _janela(df: pd.DataFrame, janela_dias: int) -> tuple[date, date]:
    fim = _hoje(df)
    return fim - timedelta(days=janela_dias - 1), fim


def _parse_data(valor: str | None) -> date | None:
    if not valor:
        return None
    return date.fromisoformat(str(valor)[:10])


def _faixa_horaria(hora: int) -> str:
    if 11 <= hora <= 14:
        return "almoço (11h-14h)"
    if 15 <= hora <= 18:
        return "tarde (15h-18h)"
    if 19 <= hora <= 22:
        return "jantar (19h-22h)"
    return "outros horários"


def _metricas(g: pd.DataFrame) -> dict[str, Any]:
    entregues = g[g["status"] == "entregue"]
    n = len(g)
    return {
        "pedidos": int(n),
        "taxa_no_prazo": _r(entregues["no_prazo"].mean()),
        "tempo_medio_min": _r(entregues["tempo_total_min"].mean(), 1),
        "tempo_p90_min": _r(entregues["tempo_total_min"].quantile(0.9), 1),
        "taxa_cancelamento": _r((g["status"] == "cancelado").mean()),
        "nota_media": _r(g["nota"].mean(), 2),
        "faturamento": _r(entregues["valor_pedido"].sum(), 2),
    }


def _r(x: Any, casas: int = 3) -> float | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), casas)


# ---------------------------------------------------------------------------
# 1. Descrever os dados
# ---------------------------------------------------------------------------
def descrever_dados() -> dict[str, Any]:
    """Resumo do que existe na base: período, regiões, restaurantes, colunas."""
    df = _df()
    rest = (
        df.groupby(["restaurante", "regiao", "culinaria"]).size().reset_index(name="pedidos")
    )
    return {
        "periodo": {"inicio": str(min(df["data"])), "fim": str(_hoje(df))},
        "total_pedidos": int(len(df)),
        "regioes": sorted(df["regiao"].unique().tolist()),
        "restaurantes": rest.to_dict(orient="records"),
        "colunas_tabela_pedidos": list(df.columns),
    }


# ---------------------------------------------------------------------------
# 2. KPIs
# ---------------------------------------------------------------------------
DIMENSOES_VALIDAS = {"data", "hora", "regiao", "restaurante", "culinaria", "faixa_horaria", "dia_semana"}


def calcular_kpis(
    data_inicio: str | None = None,
    data_fim: str | None = None,
    regiao: str | None = None,
    restaurante: str | None = None,
    agrupar_por: str | None = None,
    hora_inicio: int | None = None,
    hora_fim: int | None = None,
) -> dict[str, Any]:
    """KPIs operacionais (taxa no prazo, tempo médio, p90, cancelamento, nota)."""
    df = _df()
    ini = _parse_data(data_inicio) or _janela(df, 7)[0]
    fim = _parse_data(data_fim) or _hoje(df)
    f = df[(df["data"] >= ini) & (df["data"] <= fim)]
    if regiao:
        f = f[f["regiao"].str.lower() == regiao.lower()]
    if restaurante:
        f = f[f["restaurante"].str.lower() == restaurante.lower()]
    if hora_inicio is not None:
        f = f[f["hora"] >= int(hora_inicio)]
    if hora_fim is not None:
        f = f[f["hora"] <= int(hora_fim)]

    filtros = {
        "data_inicio": str(ini), "data_fim": str(fim), "regiao": regiao,
        "restaurante": restaurante, "hora_inicio": hora_inicio, "hora_fim": hora_fim,
    }
    if f.empty:
        return {"filtros": filtros, "erro": "Nenhum pedido encontrado com esses filtros."}

    if not agrupar_por:
        return {"filtros": filtros, "kpis": _metricas(f)}

    if agrupar_por not in DIMENSOES_VALIDAS:
        return {"erro": f"agrupar_por inválido. Use um de: {sorted(DIMENSOES_VALIDAS)}"}
    f = f.copy()
    if agrupar_por == "faixa_horaria":
        f["faixa_horaria"] = f["hora"].map(_faixa_horaria)
    grupos = [{agrupar_por: str(k), **_metricas(g)} for k, g in f.groupby(agrupar_por)]
    if agrupar_por in {"regiao", "restaurante", "culinaria", "faixa_horaria"}:
        grupos.sort(key=lambda x: (x["taxa_no_prazo"] is None, x["taxa_no_prazo"]))
    limite = 40 if agrupar_por == "restaurante" else 400
    return {"filtros": filtros, "agrupado_por": agrupar_por, "total_grupos": len(grupos), "grupos": grupos[:limite]}


# ---------------------------------------------------------------------------
# 3. Detecção de anomalias (janela recente x linha de base)
# ---------------------------------------------------------------------------
METRICAS = {
    # métrica: (tipo, coluna, "piorar" significa subir?)
    "taxa_no_prazo": ("taxa", "no_prazo", False),
    "taxa_cancelamento": ("taxa", "cancelado", True),
    "tempo_medio": ("media", "tempo_total_min", True),
    "nota_media": ("media", "nota", False),
}


def detectar_anomalias(
    metrica: str = "taxa_no_prazo",
    dimensao: str = "restaurante",
    janela_dias: int = 7,
    z_min: float = 3.0,
    excluir_datas: list[str] | None = None,
) -> dict[str, Any]:
    """Compara os últimos `janela_dias` com o período anterior e devolve as
    entidades que PIORARAM de forma estatisticamente relevante (|z| >= z_min).

    dimensao: restaurante | regiao | regiao_faixa_horaria | data
    """
    if metrica not in METRICAS:
        return {"erro": f"metrica inválida. Use: {list(METRICAS)}"}
    tipo, coluna, piora_sobe = METRICAS[metrica]
    df = _df().copy()
    df["cancelado"] = (df["status"] == "cancelado").astype(int)
    if coluna in ("no_prazo", "tempo_total_min"):
        df = df[df["status"] == "entregue"]
    df = df.dropna(subset=[coluna])
    ini_janela, fim = _janela(df, janela_dias)
    if excluir_datas:
        fora = {_parse_data(d) for d in excluir_datas}
        df = df[~df["data"].isin(fora)]
    df["na_janela"] = df["data"] >= ini_janela

    if dimensao == "data":
        return _anomalias_por_dia(df, coluna, piora_sobe, ini_janela, z_min, metrica)

    if dimensao == "regiao_faixa_horaria":
        df["entidade"] = df["regiao"] + " · " + df["hora"].map(_faixa_horaria)
    elif dimensao in ("restaurante", "regiao"):
        df["entidade"] = df[dimensao]
    else:
        return {"erro": "dimensao inválida. Use restaurante, regiao, regiao_faixa_horaria ou data."}

    achados = []
    for ent, g in df.groupby("entidade"):
        base, atual = g[~g["na_janela"]][coluna], g[g["na_janela"]][coluna]
        if len(base) < 30 or len(atual) < 15:
            continue
        mb, ma = base.mean(), atual.mean()
        if tipo == "taxa":
            p = (base.sum() + atual.sum()) / (len(base) + len(atual))
            ep = math.sqrt(max(p * (1 - p), 1e-9) * (1 / len(base) + 1 / len(atual)))
        else:
            ep = math.sqrt(base.var(ddof=1) / len(base) + atual.var(ddof=1) / len(atual))
        z = (ma - mb) / ep if ep > 0 else 0.0
        piorou = z > 0 if piora_sobe else z < 0
        if piorou and abs(z) >= z_min:
            achados.append({
                "entidade": ent,
                "linha_de_base": _r(mb), "janela_recente": _r(ma),
                "variacao_absoluta": _r(ma - mb),
                "z_score": _r(z, 1),
                "pedidos_janela": int(len(atual)),
            })
    achados.sort(key=lambda x: -abs(x["z_score"]))
    return {
        "metrica": metrica, "dimensao": dimensao,
        "janela_recente": f"{ini_janela} a {fim}",
        "linha_de_base": f"{min(df['data'])} a {ini_janela - timedelta(days=1)}",
        "criterio": f"piora com |z| >= {z_min}",
        "datas_excluidas": excluir_datas or [],
        "anomalias": achados[:15],
        "total_anomalias": len(achados),
    }


def _anomalias_por_dia(df, coluna, piora_sobe, ini_janela, z_min, metrica):
    diario = df.groupby("data")[coluna].mean()
    base = diario[diario.index < ini_janela]
    mu, sd = base.mean(), base.std(ddof=1)
    achados = []
    for dia, v in diario[diario.index >= ini_janela].items():
        z = (v - mu) / sd if sd > 0 else 0.0
        if (z > 0 if piora_sobe else z < 0) and abs(z) >= z_min:
            achados.append({"entidade": str(dia), "linha_de_base": _r(mu), "valor_dia": _r(v), "z_score": _r(z, 1)})
    achados.sort(key=lambda x: -abs(x["z_score"]))
    return {"metrica": metrica, "dimensao": "data", "criterio": f"|z| >= {z_min} vs. dias anteriores",
            "anomalias": achados, "total_anomalias": len(achados)}


# ---------------------------------------------------------------------------
# 4. Decomposição do tempo de entrega (onde está o atraso?)
# ---------------------------------------------------------------------------
ETAPAS = {
    "preparo_min": "preparo no restaurante",
    "espera_entregador_min": "espera por entregador",
    "trajeto_min": "trajeto até o cliente",
}


def decompor_tempo_entrega(
    regiao: str | None = None,
    restaurante: str | None = None,
    hora_inicio: int | None = None,
    hora_fim: int | None = None,
    janela_dias: int = 7,
) -> dict[str, Any]:
    """Quebra o tempo de entrega em preparo / espera por entregador / trajeto e
    mostra qual etapa explica a piora na janela recente."""
    df = _df()
    df = df[df["status"] == "entregue"]
    if regiao:
        df = df[df["regiao"].str.lower() == regiao.lower()]
    if restaurante:
        df = df[df["restaurante"].str.lower() == restaurante.lower()]
    if hora_inicio is not None:
        df = df[df["hora"] >= int(hora_inicio)]
    if hora_fim is not None:
        df = df[df["hora"] <= int(hora_fim)]
    if df.empty:
        return {"erro": "Nenhum pedido entregue com esses filtros."}
    ini, fim = _janela(_df(), janela_dias)
    base, atual = df[df["data"] < ini], df[df["data"] >= ini]
    etapas = []
    for col, nome in ETAPAS.items():
        b, a = base[col].mean(), atual[col].mean()
        etapas.append({"etapa": nome, "linha_de_base_min": _r(b, 1), "janela_recente_min": _r(a, 1), "variacao_min": _r(a - b, 1)})
    total_var = sum(e["variacao_min"] or 0 for e in etapas)
    for e in etapas:
        e["participacao_na_piora"] = _r((e["variacao_min"] or 0) / total_var, 2) if total_var > 0.5 else None
    principal = max(etapas, key=lambda e: e["variacao_min"] or -999)
    return {
        "filtros": {"regiao": regiao, "restaurante": restaurante, "hora_inicio": hora_inicio, "hora_fim": hora_fim},
        "janela_recente": f"{ini} a {fim}",
        "tempo_total_base_min": _r(base["tempo_total_min"].mean(), 1),
        "tempo_total_recente_min": _r(atual["tempo_total_min"].mean(), 1),
        "etapas": etapas,
        "principal_causa": principal["etapa"] if (principal["variacao_min"] or 0) > 1 else "sem piora relevante",
    }


# ---------------------------------------------------------------------------
# 5. Avaliações de clientes (voz do cliente)
# ---------------------------------------------------------------------------
TEMAS = {
    "atraso": r"demor|atras|eternidade|tempo de entrega",
    "comida fria": r"fri[oa]|murch|mole",
    "item faltando": r"faltando|faltou|esquece",
    "item indisponível": r"indispon|acabou|não tinha",
    "entregador": r"entregador",
    "elogio": r"rápido|perfeito|ótimo|excelente|muito bom|educado",
}


def classificar_tema(texto: str) -> list[str]:
    t = (texto or "").lower()
    return [tema for tema, padrao in TEMAS.items() if re.search(padrao, t)]


def analisar_avaliacoes(
    restaurante: str | None = None,
    regiao: str | None = None,
    janela_dias: int = 7,
    nota_max: int = 3,
    amostra: int = 10,
) -> dict[str, Any]:
    """Temas das avaliações negativas na janela recente vs. período anterior,
    com uma amostra de comentários reais para o agente citar."""
    df = _df()
    df = df[df["avaliacao"].fillna("") != ""]
    if restaurante:
        df = df[df["restaurante"].str.lower() == restaurante.lower()]
    if regiao:
        df = df[df["regiao"].str.lower() == regiao.lower()]
    ini, fim = _janela(_df(), janela_dias)

    def resumo(g: pd.DataFrame) -> dict[str, Any]:
        neg = g[g["nota"] <= nota_max]
        cont: dict[str, int] = {}
        for txt in neg["avaliacao"]:
            for tema in classificar_tema(txt):
                cont[tema] = cont.get(tema, 0) + 1
        return {
            "avaliacoes": int(len(g)),
            "negativas": int(len(neg)),
            "pct_negativas": _r(len(neg) / len(g)) if len(g) else None,
            "temas_negativos": dict(sorted(cont.items(), key=lambda kv: -kv[1])),
        }

    recente = df[df["data"] >= ini]
    neg_rec = recente[recente["nota"] <= nota_max]
    return {
        "filtros": {"restaurante": restaurante, "regiao": regiao, "nota_max": nota_max},
        "janela_recente": f"{ini} a {fim}",
        "recente": resumo(recente),
        "linha_de_base": resumo(df[df["data"] < ini]),
        "amostra_comentarios_negativos": neg_rec.sort_values("criado_em", ascending=False)[
            ["data", "restaurante", "nota", "avaliacao"]
        ].head(amostra).astype(str).to_dict(orient="records"),
    }


# ---------------------------------------------------------------------------
# 6. SQL somente leitura (para perguntas que as outras ferramentas não cobrem)
# ---------------------------------------------------------------------------
_PROIBIDO = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum)\b", re.I)


def validar_sql(sql: str) -> str | None:
    """Devolve uma mensagem de erro se a consulta não for um SELECT seguro."""
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        return "Apenas uma consulta por vez."
    if not re.match(r"^(select|with)\b", s, re.I):
        return "Apenas consultas SELECT são permitidas."
    if _PROIBIDO.search(s):
        return "A consulta contém comandos não permitidos."
    return None


def consultar_sql(sql: str) -> dict[str, Any]:
    """Executa um SELECT na tabela `pedidos` (somente leitura, até 200 linhas)."""
    erro = validar_sql(sql)
    if erro:
        return {"erro": erro}
    caminho = garantir_banco()
    uri = f"file:{caminho}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as con:
            df = pd.read_sql(sql.strip().rstrip(";"), con)
    except Exception as e:  # noqa: BLE001 - devolvemos o erro para o agente corrigir a query
        return {"erro": f"Erro no SQL: {e}"}
    truncado = len(df) > LIMITE_LINHAS_SQL
    return {
        "linhas": int(len(df)),
        "truncado": truncado,
        "resultado": json.loads(df.head(LIMITE_LINHAS_SQL).to_json(orient="records", force_ascii=False)),
    }


# ---------------------------------------------------------------------------
# Registro de ferramentas (schemas no formato JSON Schema)
# ---------------------------------------------------------------------------
_P_DATA = {"type": "string", "description": "Data no formato AAAA-MM-DD."}
_P_JANELA = {"type": "integer", "description": "Tamanho da janela recente em dias (padrão 7).", "default": 7}

FERRAMENTAS: list[dict[str, Any]] = [
    {
        "name": "descrever_dados",
        "description": "Mostra o período disponível, a lista de regiões, restaurantes (com região e culinária) e as colunas da tabela. Use quando precisar saber nomes exatos.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "calcular_kpis",
        "description": "Calcula KPIs operacionais: pedidos, taxa_no_prazo (equivalente ao OTIF), tempo médio e p90 de entrega, taxa de cancelamento, nota média e faturamento. Filtra por período, região, restaurante e faixa de horas; pode agrupar por data, hora, regiao, restaurante, culinaria, faixa_horaria ou dia_semana. Sem datas, usa os últimos 7 dias.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data_inicio": _P_DATA, "data_fim": _P_DATA,
                "regiao": {"type": "string"}, "restaurante": {"type": "string"},
                "agrupar_por": {"type": "string", "enum": sorted(DIMENSOES_VALIDAS)},
                "hora_inicio": {"type": "integer", "minimum": 0, "maximum": 23},
                "hora_fim": {"type": "integer", "minimum": 0, "maximum": 23},
            },
        },
    },
    {
        "name": "detectar_anomalias",
        "description": "Encontra o que PIOROU de forma estatisticamente relevante na janela recente em comparação ao período anterior (teste z). Use como primeiro passo para perguntas do tipo 'o que deu errado?' ou 'onde devo agir?'. dimensao=data encontra dias atípicos (ex.: chuva).",
        "input_schema": {
            "type": "object",
            "properties": {
                "metrica": {"type": "string", "enum": list(METRICAS)},
                "dimensao": {"type": "string", "enum": ["restaurante", "regiao", "regiao_faixa_horaria", "data"]},
                "janela_dias": _P_JANELA,
                "z_min": {"type": "number", "description": "Limiar de |z| (padrão 3).", "default": 3},
                "excluir_datas": {"type": "array", "items": {"type": "string"}, "description": "Datas atípicas (AAAA-MM-DD) a ignorar, ex.: um dia de chuva já identificado, para isolar problemas estruturais."},
            },
            "required": ["metrica", "dimensao"],
        },
    },
    {
        "name": "decompor_tempo_entrega",
        "description": "Quebra o tempo de entrega em preparo no restaurante, espera por entregador e trajeto, comparando a janela recente com o período anterior. Use para descobrir a CAUSA de um atraso.",
        "input_schema": {
            "type": "object",
            "properties": {
                "regiao": {"type": "string"}, "restaurante": {"type": "string"},
                "hora_inicio": {"type": "integer"}, "hora_fim": {"type": "integer"},
                "janela_dias": _P_JANELA,
            },
        },
    },
    {
        "name": "analisar_avaliacoes",
        "description": "Analisa avaliações de clientes: % negativas, temas de reclamação (atraso, comida fria, item faltando, item indisponível, entregador) na janela recente vs. antes, e uma amostra de comentários reais.",
        "input_schema": {
            "type": "object",
            "properties": {
                "restaurante": {"type": "string"}, "regiao": {"type": "string"},
                "janela_dias": _P_JANELA,
                "nota_max": {"type": "integer", "description": "Nota máxima considerada negativa (padrão 3)."},
                "amostra": {"type": "integer", "description": "Quantos comentários devolver (padrão 10)."},
            },
        },
    },
    {
        "name": "consultar_sql",
        "description": "Executa uma consulta SQL SELECT (SQLite) na tabela `pedidos`, somente leitura, até 200 linhas. Use apenas quando as outras ferramentas não responderem. Colunas: pedido_id, criado_em, data (AAAA-MM-DD), hora, dia_semana (0=seg), restaurante_id, restaurante, culinaria, regiao, distancia_km, tempo_prometido_min, preparo_min, espera_entregador_min, trajeto_min, tempo_total_min, no_prazo (1/0), status ('entregue'/'cancelado'), motivo_cancelamento, valor_pedido, nota, avaliacao.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
]

_FUNCOES: dict[str, Callable[..., dict[str, Any]]] = {
    "descrever_dados": descrever_dados,
    "calcular_kpis": calcular_kpis,
    "detectar_anomalias": detectar_anomalias,
    "decompor_tempo_entrega": decompor_tempo_entrega,
    "analisar_avaliacoes": analisar_avaliacoes,
    "consultar_sql": consultar_sql,
}


def executar_ferramenta(nome: str, argumentos: dict[str, Any] | None) -> str:
    """Executa uma ferramenta pelo nome e devolve o resultado em JSON (str).
    Erros viram {"erro": ...} para que o agente possa se corrigir."""
    func = _FUNCOES.get(nome)
    if func is None:
        return json.dumps({"erro": f"Ferramenta desconhecida: {nome}"}, ensure_ascii=False)
    try:
        resultado = func(**(argumentos or {}))
    except TypeError as e:
        resultado = {"erro": f"Argumentos inválidos para {nome}: {e}"}
    except Exception as e:  # noqa: BLE001
        resultado = {"erro": f"Falha ao executar {nome}: {e}"}
    return json.dumps(resultado, ensure_ascii=False, default=str)


def _limpar_schema(schema: Any) -> Any:
    """Remove palavras-chave que alguns provedores rejeitam (ex.: o Gemini não
    aceita `default`)."""
    if isinstance(schema, dict):
        return {k: _limpar_schema(v) for k, v in schema.items() if k != "default"}
    if isinstance(schema, list):
        return [_limpar_schema(v) for v in schema]
    return schema


def ferramentas_formato_openai() -> list[dict[str, Any]]:
    """Converte os schemas para o formato de tools da API compatível com OpenAI
    (usado por OpenAI, Gemini, Groq, OpenRouter, Ollama...).

    Ferramentas sem parâmetros são enviadas SEM o campo `parameters`: o Gemini
    rejeita objetos com `properties` vazio ("should be non-empty for OBJECT type")."""
    tools = []
    for f in FERRAMENTAS:
        func: dict[str, Any] = {"name": f["name"], "description": f["description"]}
        if f["input_schema"].get("properties"):
            func["parameters"] = _limpar_schema(f["input_schema"])
        tools.append({"type": "function", "function": func})
    return tools
