import json
from pathlib import Path

from copiloto.ferramentas import executar_ferramenta
from copiloto.verificador import extrair_numeros, verificar

GRAVADA = json.loads((Path(__file__).parent.parent / "copiloto" / "demo_gravada.json").read_text(encoding="utf-8"))


def _resultados():
    return [executar_ferramenta(p["ferramenta"], p["argumentos"]) for p in GRAVADA["passos"]]


def test_extrai_formatos_brasileiros_e_ignora_datas():
    texto = "Em 10/11 (2026-11-10), das 19h às 22h, caiu de 88,4% para 20,7%; R$ 8.842,76 em 189 pedidos; z = -37.2; 1 a cada 5."
    valores = [n.valor for n in extrair_numeros(texto)]
    assert valores == [88.4, 20.7, 8842.76, 189.0, -37.2]


def test_execucao_real_esta_toda_fundamentada():
    v = verificar(GRAVADA["resposta"], _resultados())
    assert v.total >= 20
    assert v.nao_encontrados == []


def test_detecta_numeros_inventados():
    adulterada = GRAVADA["resposta"].replace("88,4%", "77,4%").replace("R$ 8.842,76", "R$ 9.100,00")
    v = verificar(adulterada, _resultados())
    assert {n.texto for n in v.nao_encontrados} == {"77,4%", "R$ 9.100,00"}


def test_sem_numeros():
    assert verificar("Tudo certo por aqui.", []).taxa == 1.0
