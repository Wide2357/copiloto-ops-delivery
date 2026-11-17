import json

import pytest

from copiloto import dados
from copiloto.ferramentas import (
    analisar_avaliacoes,
    calcular_kpis,
    classificar_tema,
    consultar_sql,
    decompor_tempo_entrega,
    detectar_anomalias,
    executar_ferramenta,
    validar_sql,
)
from copiloto.relatorio import investigar

DIA_CHUVA = str(dados.DIA_CHUVA)


# --- segurança do SQL -------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "DROP TABLE pedidos",
    "DELETE FROM pedidos",
    "SELECT 1; DROP TABLE pedidos",
    "UPDATE pedidos SET nota = 5",
    "PRAGMA table_info(pedidos)",
    "WITH x AS (SELECT 1) DELETE FROM pedidos",
])
def test_sql_bloqueia_escrita(sql):
    assert validar_sql(sql) is not None
    assert "erro" in consultar_sql(sql)


def test_sql_select_funciona_e_limita_linhas():
    r = consultar_sql("SELECT * FROM pedidos")
    assert r["truncado"] is True
    assert len(r["resultado"]) == 200


def test_sql_erro_de_sintaxe_vira_mensagem():
    assert "erro" in consultar_sql("SELECT colunaquenaoexiste FROM pedidos")


# --- KPIs -------------------------------------------------------------------
def test_kpis_padrao_e_ultima_semana():
    r = calcular_kpis()
    assert r["filtros"]["data_fim"] == str(dados.DATA_FIM)
    k = r["kpis"]
    assert 0 < k["taxa_no_prazo"] < 1
    assert k["pedidos"] > 1000


def test_kpis_agrupado_ordena_pior_primeiro():
    g = calcular_kpis(agrupar_por="regiao")["grupos"]
    taxas = [x["taxa_no_prazo"] for x in g]
    assert taxas == sorted(taxas)


def test_kpis_dimensao_invalida():
    assert "erro" in calcular_kpis(agrupar_por="cor_do_capacete")


# --- anomalias: o agente precisa conseguir achar os problemas plantados -----
def test_detecta_dia_de_chuva():
    r = detectar_anomalias("taxa_no_prazo", "data")
    assert r["anomalias"][0]["entidade"] == DIA_CHUVA


def test_detecta_restaurante_com_preparo_lento():
    r = detectar_anomalias("taxa_no_prazo", "restaurante", excluir_datas=[DIA_CHUVA])
    assert r["anomalias"][0]["entidade"] == dados.RESTAURANTE_PREPARO_LENTO


def test_detecta_regiao_sem_entregador_no_jantar():
    r = detectar_anomalias("taxa_no_prazo", "regiao_faixa_horaria", excluir_datas=[DIA_CHUVA])
    assert r["anomalias"][0]["entidade"].startswith(dados.REGIAO_FALTA_ENTREGADOR)
    assert "jantar" in r["anomalias"][0]["entidade"]


def test_detecta_ruptura_por_cancelamento():
    r = detectar_anomalias("taxa_cancelamento", "restaurante", excluir_datas=[DIA_CHUVA])
    assert [a["entidade"] for a in r["anomalias"]] == [dados.RESTAURANTE_RUPTURA]


# --- causa raiz -------------------------------------------------------------
def test_decomposicao_aponta_preparo_no_restaurante():
    r = decompor_tempo_entrega(restaurante=dados.RESTAURANTE_PREPARO_LENTO)
    assert r["principal_causa"] == "preparo no restaurante"


def test_decomposicao_aponta_entregador_na_regiao():
    r = decompor_tempo_entrega(regiao=dados.REGIAO_FALTA_ENTREGADOR, hora_inicio=19, hora_fim=22)
    assert r["principal_causa"] == "espera por entregador"


def test_avaliacoes_mostram_comida_fria():
    r = analisar_avaliacoes(restaurante=dados.RESTAURANTE_PREPARO_LENTO)
    assert r["recente"]["pct_negativas"] > 5 * r["linha_de_base"]["pct_negativas"]
    assert "comida fria" in r["recente"]["temas_negativos"]


def test_classificador_de_temas():
    assert classificar_tema("Lanche chegou frio e demorou") == ["atraso", "comida fria"]
    assert classificar_tema("Chegou rápido, perfeito!") == ["elogio"]


# --- relatório offline encontra exatamente os 4 problemas -------------------
def test_relatorio_encontra_os_quatro_problemas_sem_falsos_positivos():
    alvos = [a["alvo"] for a in investigar()["achados"]]
    assert len(alvos) == 4
    assert any(dados.REGIAO_FALTA_ENTREGADOR in a for a in alvos)
    assert dados.RESTAURANTE_PREPARO_LENTO in alvos
    assert dados.RESTAURANTE_RUPTURA in alvos
    assert any(DIA_CHUVA in a for a in alvos)


# --- dispatcher -------------------------------------------------------------
def test_executar_ferramenta_desconhecida_e_args_invalidos():
    assert "erro" in json.loads(executar_ferramenta("apagar_tudo", {}))
    assert "erro" in json.loads(executar_ferramenta("calcular_kpis", {"parametro_errado": 1}))
