"""Testa o loop do agente com um LLM falso (roteirizado), sem chamar API."""

import json

from copiloto.agente import Agente, ChamadaFerramenta


class BackendRoteirizado:
    """Simula um LLM: devolve chamadas de ferramenta pré-definidas e depois um texto."""

    provedor = "fake"
    modelo = "roteiro-v1"

    def __init__(self, roteiro):
        self.roteiro = list(roteiro)
        self.resultados_recebidos = []

    def preparar(self, system, historico, pergunta):
        assert "Copiloto de Operações" in system
        return [{"role": "user", "content": pergunta}]

    def chamar(self, mensagens):
        if not self.roteiro:
            return "resposta final forçada", [], None
        passo = self.roteiro.pop(0)
        if isinstance(passo, str):
            return passo, [], None
        return None, [ChamadaFerramenta(f"id{i}", n, a) for i, (n, a) in enumerate(passo)], passo

    def anexar(self, mensagens, msg, resultados):
        self.resultados_recebidos.extend(json.loads(r) for _, r in resultados)


def test_agente_executa_ferramentas_e_devolve_texto():
    backend = BackendRoteirizado([
        [("detectar_anomalias", {"metrica": "taxa_no_prazo", "dimensao": "data"})],
        [("decompor_tempo_entrega", {"restaurante": "Sabor da Vila Burger"}),
         ("analisar_avaliacoes", {"restaurante": "Sabor da Vila Burger"})],
        "O preparo do Sabor da Vila Burger dobrou.",
    ])
    passos_vistos = []
    r = Agente(backend).perguntar("o que houve?", ao_passo=passos_vistos.append)

    assert r.texto == "O preparo do Sabor da Vila Burger dobrou."
    assert [p.ferramenta for p in r.passos] == ["detectar_anomalias", "decompor_tempo_entrega", "analisar_avaliacoes"]
    assert len(passos_vistos) == 3
    assert backend.resultados_recebidos[1]["principal_causa"] == "preparo no restaurante"


def test_agente_recupera_de_erro_de_ferramenta():
    backend = BackendRoteirizado([
        [("consultar_sql", {"sql": "DELETE FROM pedidos"})],
        "Não posso alterar dados.",
    ])
    r = Agente(backend).perguntar("apague os pedidos")
    assert "erro" in backend.resultados_recebidos[0]
    assert r.texto == "Não posso alterar dados."


def test_agente_respeita_limite_de_passos():
    loop_infinito = [[("descrever_dados", {})]] * 50
    r = Agente(BackendRoteirizado(loop_infinito), max_passos=3).perguntar("?")
    assert r.atingiu_limite
    assert len(r.passos) == 3


def test_schemas_compativeis_com_gemini():
    """O Gemini rejeita `properties` vazio e a palavra-chave `default`."""
    from copiloto.ferramentas import ferramentas_formato_openai

    tools = ferramentas_formato_openai()
    assert len(tools) == 6
    for t in tools:
        params = t["function"].get("parameters")
        if params is not None:
            assert params["properties"], t["function"]["name"]
        assert '"default"' not in json.dumps(t)
    sem_params = next(t for t in tools if t["function"]["name"] == "descrever_dados")
    assert "parameters" not in sem_params["function"]


def test_backend_openai_preserva_campos_extras_do_provedor():
    """Campos extras (ex.: thought signature do Gemini) precisam voltar intactos."""
    from openai.types.chat import ChatCompletionMessage

    from copiloto.agente import BackendOpenAICompat, ChamadaFerramenta

    msg = ChatCompletionMessage.model_validate({
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "descrever_dados", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "abc123"}},
        }],
    })
    b = BackendOpenAICompat.__new__(BackendOpenAICompat)
    mensagens = []
    b.anexar(mensagens, msg, [(ChamadaFerramenta("c1", "descrever_dados", {}), "{}")])
    assert mensagens[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "abc123"
    assert mensagens[1] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}
