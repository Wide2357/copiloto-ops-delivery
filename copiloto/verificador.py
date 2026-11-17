"""Verificador de números: a resposta do agente está fundamentada nos dados?

Extrai todos os números relevantes da resposta (percentuais, valores em R$,
minutos, z-scores...) e confere se cada um aparece nos resultados das
ferramentas que o agente chamou. Um número que não aparece em lugar nenhum é
marcado como "não encontrado": pode ser uma conta que o modelo fez sozinho
(ex.: uma diferença) ou uma alucinação. Em ambos os casos, vale revisar.

É uma checagem simples e determinística, sem LLM: barata o suficiente para
rodar em toda resposta.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# trechos que não são métricas: datas, horários, anos, marcadores de lista
_IGNORAR = [
    r"\b\d{4}-\d{2}-\d{2}\b",              # 2026-11-10
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",   # 10/11, 08/11/2026
    r"\b\d{1,2}h(?:\d{2})?\b",             # 19h, 19h30
    r"\b20\d{2}\b",                         # anos
    r"(?m)^\s*(?:#+\s*)?\d+\.\s",          # "1. " no início da linha
]

_NUMERO = re.compile(
    r"(?P<moeda>R\$\s?)?"
    r"(?P<num>[-−]?\d{1,3}(?:\.\d{3})+(?:,\d+)?|[-−]?\d+(?:[.,]\d+)?)"
    r"(?P<pct>\s?%)?"
)


@dataclass
class Numero:
    texto: str
    valor: float
    casas: int
    percentual: bool
    contexto: str


@dataclass
class Verificacao:
    numeros: list[Numero] = field(default_factory=list)
    conferidos: list[Numero] = field(default_factory=list)
    nao_encontrados: list[Numero] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.numeros)

    @property
    def taxa(self) -> float:
        return len(self.conferidos) / self.total if self.total else 1.0

    def resumo(self) -> str:
        if not self.total:
            return "Nenhum número para conferir."
        return f"{len(self.conferidos)} de {self.total} números encontrados nos resultados das ferramentas ({self.taxa:.0%})"


def _para_float(s: str) -> tuple[float, int]:
    s = s.replace("−", "-")
    if "," in s:  # formato brasileiro: 8.051,05
        inteiro, dec = s.replace(".", "").split(",")
        return float(f"{inteiro}.{dec}"), len(dec)
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", s):  # 27.774 = milhar
        return float(s.replace(".", "")), 0
    casas = len(s.split(".")[1]) if "." in s else 0
    return float(s), casas


def extrair_numeros(texto: str) -> list[Numero]:
    """Extrai os números relevantes de uma resposta em português."""
    limpo = texto
    for padrao in _IGNORAR:
        limpo = re.sub(padrao, lambda m: " " * len(m.group(0)), limpo)
    numeros = []
    for m in _NUMERO.finditer(limpo):
        valor, casas = _para_float(m.group("num"))
        pct = bool(m.group("pct"))
        # contagens pequenas ("4 problemas", "1 a cada 5") não são métricas
        if not pct and not m.group("moeda") and casas == 0 and abs(valor) <= 10:
            continue
        ini, fim = max(0, m.start() - 40), min(len(texto), m.end() + 20)
        numeros.append(Numero(m.group(0).strip(), valor, casas, pct, texto[ini:fim].replace("\n", " ").strip()))
    return numeros


def coletar_valores(resultados: Iterable[str | dict | list]) -> list[float]:
    """Todos os números presentes nos resultados (JSON) das ferramentas."""
    valores: list[float] = []

    def andar(x: Any) -> None:
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            valores.append(float(x))
        elif isinstance(x, str):
            try:
                valores.append(float(x))
            except ValueError:
                pass
        elif isinstance(x, dict):
            for v in x.values():
                andar(v)
        elif isinstance(x, list):
            for v in x:
                andar(v)

    for r in resultados:
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except json.JSONDecodeError:
                continue
        andar(r)
    return valores


def _confere(n: Numero, valores: list[float]) -> bool:
    tol = 0.5 * 10 ** (-n.casas) + 1e-9  # aceita arredondamento do texto
    alvo = abs(n.valor)
    for v in valores:
        if abs(abs(v) - alvo) <= tol:
            return True
        if n.percentual and abs(abs(v) * 100 - alvo) <= tol + 0.051:  # 0.903 ↔ 90,3%
            return True
    return False


def verificar(texto: str, resultados: Iterable[str | dict | list]) -> Verificacao:
    valores = coletar_valores(resultados)
    v = Verificacao(numeros=extrair_numeros(texto))
    for n in v.numeros:
        (v.conferidos if _confere(n, valores) else v.nao_encontrados).append(n)
    return v
