"""Copiloto de Operações de Delivery: interface Streamlit.

Rodar:  streamlit run app.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from copiloto.agente import PROVEDORES, Agente, Passo, criar_backend
from copiloto.ferramentas import _df, _hoje, calcular_kpis, detectar_anomalias
from copiloto.ferramentas import executar_ferramenta
from copiloto.relatorio import PERGUNTA_RELATORIO, relatorio_deterministico
from copiloto.verificador import verificar

VERMELHO = "#EA1D2C"
CINZA = "#9AA0A6"

st.set_page_config(page_title="Copiloto de Operações", page_icon="🛵", layout="wide")

PERGUNTAS_SUGERIDAS = [
    "O que piorou na última semana e onde devo agir primeiro?",
    "Por que o Sabor da Vila Burger está atrasando?",
    "A região de Tatuapé tem problema de restaurante ou de entregador?",
    "Quanto faturamento foi afetado pelos cancelamentos do Sushi Hikari?",
    "O que aconteceu no dia 10/11?",
]


# ---------------------------------------------------------------------------
# Barra lateral: provedor de IA
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🛵 Copiloto de Operações")
    st.caption("Agente de IA que investiga a operação de delivery e recomenda ações.")
    provedor = st.selectbox("Provedor de IA", list(PROVEDORES), index=list(PROVEDORES).index("gemini"), format_func=lambda p: {"anthropic": "Claude (Anthropic)", "openai": "GPT (OpenAI)", "gemini": "Gemini (Google, com camada gratuita)"}[p])
    cfg = PROVEDORES[provedor]
    modelo = st.text_input("Modelo", value=os.getenv(f"{provedor.upper()}_MODEL", cfg["modelo"] or ""))
    chave_env = os.getenv(cfg["env_chave"] or "", "")
    chave = st.text_input("Chave de API", value="", type="password", placeholder="usando variável de ambiente" if chave_env else "cole sua chave aqui")
    chave = chave or chave_env
    st.caption("A chave fica só na sua sessão; não é salva em lugar nenhum.")
    st.divider()
    st.caption("Dados sintéticos: 60 dias, ~28 mil pedidos em 8 regiões de São Paulo, com problemas plantados na última semana para o agente descobrir.")


def obter_agente() -> Agente | None:
    if not chave:
        return None
    try:
        return Agente(criar_backend(provedor, chave, modelo or None))
    except Exception as e:  # noqa: BLE001
        st.error(f"Não foi possível iniciar o provedor: {e}")
        return None


def explicar_erro(e: Exception) -> str:
    msg = str(e)
    if "429" in msg or "quota" in msg.lower() or "rate" in msg.lower():
        return "Limite de uso do provedor atingido (comum na camada gratuita). Aguarde cerca de 1 minuto e tente de novo."
    if "503" in msg or "500" in msg or "overloaded" in msg.lower() or "unavailable" in msg.lower():
        return "O servidor do provedor de IA está sobrecarregado no momento. Tente de novo em 1-2 minutos ou troque o modelo na barra lateral (ex.: gemini-2.5-flash)."
    if "401" in msg or "API key" in msg or "invalid" in msg.lower():
        return "Chave de API inválida. Confira a chave na barra lateral."
    if "404" in msg:
        return "Modelo não encontrado. Troque o nome do modelo na barra lateral."
    return f"O provedor de IA retornou um erro: {msg[:300]}"


def mostrar_passos(passos: list[Passo]) -> None:
    with st.expander(f"🔎 Como cheguei nisso: {len(passos)} chamadas de ferramenta"):
        for i, p in enumerate(passos, 1):
            st.markdown(f"**{i}. `{p.ferramenta}`** · {p.duracao_ms} ms")
            st.code(json.dumps(p.argumentos, ensure_ascii=False, indent=2), language="json")
            try:
                st.json(json.loads(p.resultado), expanded=False)
            except json.JSONDecodeError:
                st.text(p.resultado[:2000])


def mostrar_verificacao(texto: str, passos: list[Passo]) -> None:
    v = verificar(texto, [p.resultado for p in passos])
    if not v.total:
        return
    icone = "✅" if not v.nao_encontrados else "⚠️"
    with st.expander(f"{icone} Verificação automática: {v.resumo()}"):
        st.caption("Cada número da resposta é procurado nos resultados das ferramentas. "
                   "Um número não encontrado pode ser uma conta feita pelo modelo ou uma alucinação: vale revisar.")
        if v.nao_encontrados:
            for n in v.nao_encontrados:
                st.markdown(f"- **{n.texto}** não aparece nos dados → _…{n.contexto}…_")
        else:
            st.markdown("Todos os números conferem: " + ", ".join(f"`{n.texto}`" for n in v.conferidos))


@st.cache_data
def carregar_demo() -> dict:
    return json.loads((Path(__file__).parent / "copiloto" / "demo_gravada.json").read_text(encoding="utf-8"))


def reproduzir_demo() -> None:
    """Reproduz uma execução real gravada: as ferramentas rodam de verdade,
    a resposta é a que o modelo escreveu naquela execução."""
    demo = carregar_demo()
    st.session_state.historico.append({"role": "user", "content": demo["pergunta"]})
    with st.chat_message("user"):
        st.markdown(demo["pergunta"])
    with st.chat_message("assistant", avatar="🛵"):
        status = st.status("Reproduzindo a investigação…", expanded=True)
        passos = []
        for p in demo["passos"]:
            t0 = time.time()
            resultado = executar_ferramenta(p["ferramenta"], p["argumentos"])
            passos.append(Passo(p["ferramenta"], p["argumentos"], resultado, int((time.time() - t0) * 1000)))
            status.write(f"🔧 `{p['ferramenta']}` {json.dumps(p['argumentos'], ensure_ascii=False)}")
            time.sleep(0.35)
        status.update(label=f"Execução real gravada · {demo['modelo']} · {len(passos)} ferramentas · {demo['duracao_s']}s", state="complete", expanded=False)
        st.markdown(demo["resposta"])
        st.caption(f"🎬 {demo['descricao']}")
        mostrar_passos(passos)
        mostrar_verificacao(demo["resposta"], passos)
    st.session_state.historico.append({"role": "assistant", "content": demo["resposta"], "passos": passos, "demo": True})


aba_painel, aba_chat, aba_rel = st.tabs(["📊 Painel", "🤖 Copiloto", "📝 Relatório semanal"])

# ---------------------------------------------------------------------------
# Painel
# ---------------------------------------------------------------------------
with aba_painel:
    df = _df()
    fim = _hoje(df)
    semana = calcular_kpis()["kpis"]
    antes = calcular_kpis(data_inicio=str(fim - timedelta(days=34)), data_fim=str(fim - timedelta(days=7)))["kpis"]

    st.subheader(f"Última semana ({fim - timedelta(days=6):%d/%m} a {fim:%d/%m}) vs. 4 semanas anteriores")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Taxa no prazo", f"{semana['taxa_no_prazo']:.1%}", f"{(semana['taxa_no_prazo'] - antes['taxa_no_prazo']) * 100:+.1f} p.p.")
    c2.metric("Tempo médio", f"{semana['tempo_medio_min']:.1f} min", f"{semana['tempo_medio_min'] - antes['tempo_medio_min']:+.1f} min", delta_color="inverse")
    c3.metric("Cancelamento", f"{semana['taxa_cancelamento']:.1%}", f"{(semana['taxa_cancelamento'] - antes['taxa_cancelamento']) * 100:+.1f} p.p.", delta_color="inverse")
    c4.metric("Nota média", f"{semana['nota_media']:.2f}", f"{semana['nota_media'] - antes['nota_media']:+.2f}")

    diario = pd.DataFrame(calcular_kpis(data_inicio=str(min(df["data"])), agrupar_por="data")["grupos"])
    diario["data"] = pd.to_datetime(diario["data"])
    fig = px.line(diario, x="data", y="taxa_no_prazo", title="Taxa de entrega no prazo por dia", markers=True)
    fig.update_traces(line_color=VERMELHO)
    fig.update_layout(yaxis_tickformat=".0%", yaxis_title=None, xaxis_title=None, height=320, margin=dict(t=50, b=10))
    fig.add_vrect(x0=pd.Timestamp(fim - timedelta(days=6)) - pd.Timedelta(hours=12), x1=pd.Timestamp(fim) + pd.Timedelta(hours=12), fillcolor=VERMELHO, opacity=0.07, line_width=0, annotation_text="última semana")
    st.plotly_chart(fig, use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        reg = pd.DataFrame(calcular_kpis(agrupar_por="regiao")["grupos"]).sort_values("taxa_no_prazo")
        fig2 = px.bar(reg, x="taxa_no_prazo", y="regiao", orientation="h", title="Taxa no prazo por região (última semana)")
        media = semana["taxa_no_prazo"]
        fig2.update_traces(marker_color=[VERMELHO if v < media else CINZA for v in reg["taxa_no_prazo"]])
        fig2.add_vline(x=media, line_dash="dot", line_color="#555", annotation_text="média")
        fig2.update_layout(xaxis_tickformat=".0%", xaxis_title=None, yaxis_title=None, height=340, margin=dict(t=50, b=10))
        st.plotly_chart(fig2, use_container_width=True)
    with col_b:
        st.markdown("**Alertas automáticos (teste z, última semana vs. anteriores)**")
        alertas = []
        for metrica, dim in [("taxa_no_prazo", "data"), ("taxa_no_prazo", "regiao_faixa_horaria"), ("taxa_no_prazo", "restaurante"), ("taxa_cancelamento", "restaurante")]:
            for a in detectar_anomalias(metrica, dim, z_min=6)["anomalias"][:3]:
                alertas.append({"métrica": metrica, "onde": a["entidade"], "z": a["z_score"]})
        st.dataframe(pd.DataFrame(alertas), hide_index=True, use_container_width=True, height=300)
        st.caption("Os alertas mostram ONDE piorou. Pergunte ao Copiloto POR QUÊ.")

# ---------------------------------------------------------------------------
# Chat com o agente
# ---------------------------------------------------------------------------
with aba_chat:
    if "historico" not in st.session_state:
        st.session_state.historico = []  # [{"role", "content", "passos"}]

    if not chave:
        st.info("Informe uma chave de API na barra lateral para conversar com o agente. "
                "Sem chave, você pode ver uma **execução real gravada** logo abaixo, e o **Painel** e o "
                "**Relatório semanal (modo sem IA)** continuam funcionando.")

    for m in st.session_state.historico:
        with st.chat_message(m["role"], avatar="🛵" if m["role"] == "assistant" else None):
            st.markdown(m["content"])
            if m.get("demo"):
                st.caption(f"🎬 {carregar_demo()['descricao']}")
            if m.get("passos"):
                mostrar_passos(m["passos"])
                mostrar_verificacao(m["content"], m["passos"])

    if not st.session_state.historico and st.button("▶️ Ver uma execução real do agente (não precisa de chave)", type="primary"):
        reproduzir_demo()
        st.stop()

    sugestao = None
    if not st.session_state.historico:
        st.markdown("**Experimente perguntar:**")
        cols = st.columns(len(PERGUNTAS_SUGERIDAS))
        for col, q in zip(cols, PERGUNTAS_SUGERIDAS):
            if col.button(q, use_container_width=True, disabled=not chave):
                sugestao = q

    pergunta = st.chat_input("Pergunte sobre a operação…", disabled=not chave) or sugestao
    if pergunta and (agente := obter_agente()):
        historico_llm = [{"role": m["role"], "content": m["content"]} for m in st.session_state.historico]
        st.session_state.historico.append({"role": "user", "content": pergunta})
        with st.chat_message("user"):
            st.markdown(pergunta)
        with st.chat_message("assistant", avatar="🛵"):
            status = st.status("Investigando…", expanded=True)
            try:
                resp = agente.perguntar(pergunta, historico_llm, ao_passo=lambda p: status.write(f"🔧 `{p.ferramenta}` {json.dumps(p.argumentos, ensure_ascii=False)}"))
            except Exception as e:  # noqa: BLE001 - erro do provedor (limite, chave, modelo)
                status.update(label="Erro ao consultar o provedor de IA", state="error")
                st.error(explicar_erro(e))
                st.session_state.historico.pop()  # permite refazer a pergunta
                st.stop()
            status.update(label=f"Concluído em {resp.duracao_s}s · {len(resp.passos)} ferramentas · {resp.modelo}", state="complete", expanded=False)
            st.markdown(resp.texto)
            mostrar_passos(resp.passos)
            mostrar_verificacao(resp.texto, resp.passos)
        st.session_state.historico.append({"role": "assistant", "content": resp.texto, "passos": resp.passos})

# ---------------------------------------------------------------------------
# Relatório semanal
# ---------------------------------------------------------------------------
with aba_rel:
    st.markdown("Relatório pronto para mandar ao time toda segunda-feira.")
    modo_ia = st.toggle("Escrever com IA (o agente investiga sozinho)", value=False, disabled=not chave)
    if st.button("Gerar relatório", type="primary"):
        if modo_ia and (agente := obter_agente()):
            try:
                with st.spinner("O agente está investigando a semana…"):
                    resp = agente.perguntar(PERGUNTA_RELATORIO)
            except Exception as e:  # noqa: BLE001
                st.error(explicar_erro(e))
                st.stop()
            st.session_state.relatorio = resp.texto
            st.session_state.relatorio_passos = resp.passos
        else:
            st.session_state.relatorio = relatorio_deterministico()
            st.session_state.relatorio_passos = []
    if "relatorio" in st.session_state:
        st.markdown(st.session_state.relatorio)
        if st.session_state.relatorio_passos:
            mostrar_passos(st.session_state.relatorio_passos)
        st.download_button("Baixar .md", st.session_state.relatorio, file_name=f"relatorio_ops_{_hoje(_df())}.md")
