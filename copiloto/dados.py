"""Geração e carga de dados sintéticos de pedidos de delivery.

Os dados imitam uma operação real de delivery em São Paulo (60 dias, ~25 mil
pedidos) e trazem **problemas plantados** na última semana, para que o agente
tenha algo real para investigar:

1. Um restaurante cujo tempo de preparo disparou (cozinha sobrecarregada).
2. Uma região com falta de entregadores no pico do jantar.
3. Um dia de chuva forte que atrasou todas as regiões.
4. Um restaurante com ruptura de estoque (cancelamentos por item indisponível).

Tudo é reprodutível (seed fixa), então os testes e as avaliações do agente
sempre encontram os mesmos números.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
CAMINHO_DB = RAIZ / "data" / "ops.db"

DATA_FIM = date(2026, 11, 14)
DIAS = 60
DATA_INICIO = DATA_FIM - timedelta(days=DIAS - 1)
INICIO_JANELA_PROBLEMAS = DATA_FIM - timedelta(days=6)  # últimos 7 dias
DIA_CHUVA = date(2026, 11, 10)

REGIOES = {
    # região: (peso de demanda, distância média km)
    "Pinheiros": (1.3, 2.6),
    "Vila Mariana": (1.2, 2.8),
    "Moema": (1.0, 2.5),
    "Tatuapé": (1.1, 3.2),
    "Santana": (0.8, 3.4),
    "Butantã": (0.7, 3.6),
    "Centro": (1.0, 2.2),
    "Mooca": (0.9, 3.0),
}

CULINARIAS = {
    # culinária: (preparo médio min, ticket médio R$)
    "Hambúrguer": (16, 52),
    "Japonesa": (20, 85),
    "Pizza": (22, 70),
    "Brasileira": (15, 38),
    "Árabe": (14, 42),
    "Saudável": (12, 45),
    "Doces": (8, 30),
    "Italiana": (19, 65),
}

NOMES = {
    "Hambúrguer": ["Sabor da Vila Burger", "Brasa Smash", "Burger do Zé", "Chapa Quente", "Big Bairro Burger"],
    "Japonesa": ["Sushi Hikari", "Temaki Express", "Kaizen Sushi", "Oishii Bar"],
    "Pizza": ["Forno da Nona", "Pizza Paulistana", "Massa Fina", "Bella Fatia", "Pizzaria do Largo"],
    "Brasileira": ["Marmita da Dona Cida", "Prato Feito Paulista", "Feijão de Corda", "Cantina Mineira", "Panela de Barro"],
    "Árabe": ["Esfiha Damasco", "Kebab Beirute", "Habibi Lanches"],
    "Saudável": ["Verde Bowl", "Fit Express", "Salada & Cia"],
    "Doces": ["Doce Brigadeiro", "Açaí da Praça", "Confeitaria Aurora"],
    "Italiana": ["Trattoria Sole", "Nonna Rosa", "Spaghetteria 22"],
}

# Problemas plantados (usados também pelos testes e pelas avaliações)
RESTAURANTE_PREPARO_LENTO = "Sabor da Vila Burger"
REGIAO_FALTA_ENTREGADOR = "Tatuapé"
RESTAURANTE_RUPTURA = "Sushi Hikari"

AVALIACOES = {
    "atraso": [
        "Demorou demais, chegou quase uma hora depois.",
        "Pedido atrasou muito, ninguém avisou nada.",
        "Tempo de entrega muito acima do prometido.",
        "Esperei uma eternidade, atrasou de novo.",
    ],
    "comida_fria": [
        "Lanche chegou frio e murcho.",
        "Comida fria, dava pra ver que ficou parada.",
        "Batata chegou fria e mole.",
    ],
    "item_faltando": [
        "Veio faltando um item do pedido.",
        "Esqueceram o refrigerante.",
        "Faltou o molho e a sobremesa.",
    ],
    "item_indisponivel": [
        "Cancelaram porque não tinha o item, péssimo.",
        "Pedi o combo e disseram que acabou depois de 20 min.",
        "Item indisponível de novo, já é a segunda vez.",
    ],
    "entregador": [
        "Entregador demorou pra chegar no restaurante.",
        "O app ficou procurando entregador por muito tempo.",
        "Ficou parado esperando entregador, o pedido já estava pronto.",
    ],
    "elogio": [
        "Chegou rápido e quentinho, perfeito!",
        "Muito bom, entrega no prazo.",
        "Ótimo como sempre.",
        "Comida excelente e bem embalada.",
        "Entregador super educado, tudo certo.",
    ],
}


def _montar_restaurantes(rng: np.random.Generator) -> pd.DataFrame:
    linhas = []
    regioes = list(REGIOES)
    rid = 1
    for culinaria, nomes in NOMES.items():
        for nome in nomes:
            if nome == RESTAURANTE_PREPARO_LENTO:
                regiao = "Vila Mariana"
            elif nome == RESTAURANTE_RUPTURA:
                regiao = "Pinheiros"
            else:
                regiao = regioes[rid % len(regioes)]
            linhas.append(
                {
                    "restaurante_id": rid,
                    "restaurante": nome,
                    "culinaria": culinaria,
                    "regiao": regiao,
                    "popularidade": float(rng.uniform(0.6, 1.6)),
                }
            )
            rid += 1
    return pd.DataFrame(linhas)


def gerar_pedidos(seed: int = 42, pedidos_por_dia: int = 420) -> pd.DataFrame:
    """Gera o DataFrame de pedidos com os problemas plantados."""
    rng = np.random.default_rng(seed)
    rest = _montar_restaurantes(rng)
    pesos = rest["popularidade"] * rest["regiao"].map(lambda r: REGIOES[r][0])
    pesos = (pesos / pesos.sum()).to_numpy()

    # distribuição de horário: pico de almoço e jantar
    horas = np.arange(10, 24)
    peso_hora = np.array([2, 6, 10, 8, 4, 3, 3, 4, 7, 11, 12, 9, 5, 2], dtype=float)
    peso_hora /= peso_hora.sum()

    registros = []
    pid = 100000
    for d in range(DIAS):
        dia = DATA_INICIO + timedelta(days=d)
        fim_de_semana = dia.weekday() >= 4  # sex, sáb, dom
        n = int(rng.poisson(pedidos_por_dia * (1.25 if fim_de_semana else 1.0)))
        janela_problema = dia >= INICIO_JANELA_PROBLEMAS
        idx_rest = rng.choice(len(rest), size=n, p=pesos)
        hs = rng.choice(horas, size=n, p=peso_hora)
        for i, h in zip(idx_rest, hs):
            r = rest.iloc[i]
            prep_base, ticket = CULINARIAS[r.culinaria]
            dist = max(0.4, rng.normal(REGIOES[r.regiao][1], 1.0))
            pico = h in (12, 13, 19, 20, 21)

            prep = max(4.0, rng.normal(prep_base, 4.0) + (3 if pico else 0))
            espera = max(0.0, rng.gamma(2.0, 2.0) + (2.5 if pico else 0))
            trajeto = max(4.0, dist * rng.normal(4.2, 0.6) + rng.normal(3, 1.5))

            # --- problemas plantados ---
            if janela_problema and r.restaurante == RESTAURANTE_PREPARO_LENTO:
                prep += rng.normal(16, 4)
            if janela_problema and r.regiao == REGIAO_FALTA_ENTREGADOR and h in (19, 20, 21):
                espera += rng.normal(13, 3)
            if dia == DIA_CHUVA:
                trajeto += rng.normal(6, 2.5)
                espera += rng.normal(2, 1)

            prometido = round(prep_base + 6 + dist * 4.2 + 8 + (5 if pico else 0))
            total = prep + espera + trajeto
            atraso = total - prometido

            # cancelamento
            p_cancel = 0.025 + (0.12 if atraso > 20 else 0.0)
            motivo = None
            if janela_problema and r.restaurante == RESTAURANTE_RUPTURA and rng.random() < 0.18:
                status, motivo = "cancelado", "item indisponível"
            elif rng.random() < p_cancel:
                status = "cancelado"
                motivo = rng.choice(
                    ["atraso", "desistência do cliente", "restaurante fechado", "item indisponível"],
                    p=[0.45, 0.30, 0.10, 0.15] if atraso > 20 else [0.10, 0.50, 0.15, 0.25],
                )
            else:
                status = "entregue"

            # avaliação
            nota, texto = None, ""
            if status == "entregue" and rng.random() < 0.35:
                base = 4.7 - max(0.0, atraso) * 0.07 - (0.4 if espera > 12 else 0)
                nota = int(np.clip(round(rng.normal(base, 0.6)), 1, 5))
                if rng.random() < 0.55:
                    if nota >= 4:
                        tema = "elogio"
                    elif atraso > 8 and prep > prep_base + 10:
                        tema = rng.choice(["comida_fria", "atraso"], p=[0.55, 0.45])
                    elif espera > 12:
                        tema = rng.choice(["entregador", "atraso"], p=[0.6, 0.4])
                    elif atraso > 8:
                        tema = "atraso"
                    else:
                        tema = rng.choice(["item_faltando", "comida_fria", "atraso"])
                    texto = str(rng.choice(AVALIACOES[tema]))
            elif status == "cancelado" and motivo == "item indisponível" and rng.random() < 0.5:
                nota = 1
                texto = str(rng.choice(AVALIACOES["item_indisponivel"]))

            minuto = int(rng.integers(0, 60))
            criado = datetime(dia.year, dia.month, dia.day, int(h), minuto)
            entregue = status == "entregue"
            registros.append(
                {
                    "pedido_id": pid,
                    "criado_em": criado.isoformat(sep=" "),
                    "data": dia.isoformat(),
                    "hora": int(h),
                    "dia_semana": dia.weekday(),
                    "restaurante_id": int(r.restaurante_id),
                    "restaurante": r.restaurante,
                    "culinaria": r.culinaria,
                    "regiao": r.regiao,
                    "distancia_km": round(float(dist), 2),
                    "tempo_prometido_min": int(prometido),
                    "preparo_min": round(float(prep), 1),
                    "espera_entregador_min": round(float(espera), 1),
                    "trajeto_min": round(float(trajeto), 1),
                    "tempo_total_min": round(float(total), 1) if entregue else None,
                    "no_prazo": int(atraso <= 0) if entregue else None,
                    "status": status,
                    "motivo_cancelamento": motivo,
                    "valor_pedido": round(float(max(12, rng.normal(ticket, ticket * 0.3))), 2),
                    "nota": nota,
                    "avaliacao": texto,
                }
            )
            pid += 1
    return pd.DataFrame(registros)


def criar_banco(caminho: Path = CAMINHO_DB, seed: int = 42) -> Path:
    """Gera os dados e grava no SQLite (tabela `pedidos`)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    df = gerar_pedidos(seed=seed)
    with sqlite3.connect(caminho) as con:
        df.to_sql("pedidos", con, if_exists="replace", index=False)
        con.execute("CREATE INDEX IF NOT EXISTS ix_data ON pedidos(data)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_rest ON pedidos(restaurante)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_reg ON pedidos(regiao)")
    return caminho


def garantir_banco(caminho: Path = CAMINHO_DB) -> Path:
    if not caminho.exists():
        criar_banco(caminho)
    return caminho


def carregar_pedidos(caminho: Path = CAMINHO_DB) -> pd.DataFrame:
    garantir_banco(caminho)
    with sqlite3.connect(caminho) as con:
        return pd.read_sql("SELECT * FROM pedidos", con)


if __name__ == "__main__":
    p = criar_banco()
    df = carregar_pedidos(p)
    print(f"Banco criado em {p} com {len(df):,} pedidos ({df['data'].min()} a {df['data'].max()}).")
