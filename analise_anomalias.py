"""
analise_anomalias.py — Detecção de bets com comportamento anômalo
==================================================================
Cruza dados em tempo real (`_dados`) com históricos (`url_health.json`,
`reclame_aqui_health.json`, `stats_snapshots.json`) para destacar 3 padrões:

1. **URLs caindo** — bets com ≥3 falhas em 24h registradas no histórico do url_health
   (mesmo gatilho do alerta `url_down`, mas listado no dashboard)

2. **Queda na nota RA** — bets cujo `nota` no Reclame Aqui caiu nas últimas semanas
   (compara nota atual com nota da semana anterior usando snapshot diário)

3. **Sem email + recém-adicionada** — bets sem email_contato adicionadas no
   mês corrente (oportunidades de prospecção quentes)

API pública:
    urls_caindo(registros)        -> list[dict]
    queda_ra(registros)           -> list[dict]
    novas_sem_email(registros, n) -> list[dict]
    resumo(registros)             -> dict com 3 listas + contadores
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from logging_config import get_logger

logger = get_logger(__name__)

ARQUIVO_URL_HEALTH = Path("dados/url_health.json")
ARQUIVO_RA_HEALTH  = Path("dados/reclame_aqui_health.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ler_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8")) or {}
    except Exception:
        return {}


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", ""))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Anomalia 1 — URLs caindo recorrentemente nas últimas 24h
# ---------------------------------------------------------------------------

def urls_caindo(registros: list[dict]) -> list[dict]:
    """
    Retorna bets cuja URL teve ≥3 falhas registradas no histórico (etapa 4.1).
    Ordenado por nº de falhas DESC.
    """
    health = _ler_json(ARQUIVO_URL_HEALTH)
    # Mapa URL → registro
    por_url = {(r.get("url") or "").strip(): r for r in registros if r.get("url")}

    anomalias: list[dict] = []
    for url, info in health.items():
        falhas = info.get("_historico_falhas") or []
        if len(falhas) < 3:
            continue
        reg = por_url.get(url)
        anomalias.append({
            "url":             url,
            "marca":           (reg or {}).get("marca", ""),
            "cnpj":            (reg or {}).get("cnpj", ""),
            "uf":              (reg or {}).get("uf", ""),
            "n_falhas_24h":    len(falhas),
            "primeira_falha":  falhas[0] if falhas else None,
            "ultima_falha":    falhas[-1] if falhas else None,
            "status_atual":    info.get("status", "?"),
            "http_code":       info.get("http_code", 0),
            "health_score":    (reg or {}).get("_health_score"),
        })
    anomalias.sort(key=lambda a: -a["n_falhas_24h"])
    return anomalias


# ---------------------------------------------------------------------------
# Anomalia 2 — Queda na nota Reclame Aqui (snapshots comparativos)
# ---------------------------------------------------------------------------

# Note: o worker `reclame_aqui_health` substitui a entrada inteira a cada
# verificação, então não temos histórico nativo de notas. Implementamos um
# histórico em memória que é populado quando esta função roda — a cada call
# salva o snapshot atual e compara com o anterior.

_HIST_NOTAS_RA: dict[str, list[dict]] = {}   # slug → list[{ts, nota}]
_MAX_HIST = 14   # mantém 14 entradas (~2 semanas se rodar diariamente)


def _atualizar_historico_ra(ra_health: dict) -> None:
    """Salva snapshot atual no histórico in-memory."""
    agora = datetime.now().isoformat(timespec="seconds")
    for slug, info in ra_health.items():
        if info.get("status") != "encontrado" or info.get("nota") is None:
            continue
        hist = _HIST_NOTAS_RA.setdefault(slug, [])
        # Só adiciona se a nota mudou OU se passou >1h da última entrada
        if hist and hist[-1].get("nota") == info["nota"]:
            ultima_ts = _parse_iso(hist[-1].get("ts"))
            if ultima_ts and (datetime.now() - ultima_ts).total_seconds() < 3600:
                continue
        hist.append({"ts": agora, "nota": info["nota"], "marca": info.get("marca", "")})
        if len(hist) > _MAX_HIST:
            hist.pop(0)


def queda_ra(registros: list[dict], queda_min: float = 0.3) -> list[dict]:
    """
    Retorna bets cuja nota RA caiu ≥`queda_min` em relação à 1ª entrada
    do histórico (ou à mais antiga disponível).

    Note: depende do histórico in-memory; primeiro call popula, segundo+ calls
    detectam mudanças. Sobrevive até reinício do processo.
    """
    ra_health = _ler_json(ARQUIVO_RA_HEALTH)
    _atualizar_historico_ra(ra_health)

    # Mapa marca → registro (para enriquecer)
    por_marca = {(r.get("marca") or "").lower(): r for r in registros}

    anomalias: list[dict] = []
    for slug, hist in _HIST_NOTAS_RA.items():
        if len(hist) < 2:
            continue
        nota_atual = hist[-1].get("nota")
        nota_antiga = hist[0].get("nota")
        if nota_atual is None or nota_antiga is None:
            continue
        queda = nota_antiga - nota_atual
        if queda < queda_min:
            continue
        marca = hist[-1].get("marca") or hist[0].get("marca") or slug
        reg = por_marca.get(marca.lower(), {})
        anomalias.append({
            "marca":         marca,
            "slug":          slug,
            "nota_atual":    nota_atual,
            "nota_anterior": nota_antiga,
            "queda":         round(queda, 2),
            "ts_anterior":   hist[0].get("ts"),
            "ts_atual":      hist[-1].get("ts"),
            "url_ra":        ra_health.get(slug, {}).get("url_reclame_aqui", ""),
            "health_score":  reg.get("_health_score"),
        })
    anomalias.sort(key=lambda a: -a["queda"])
    return anomalias


# ---------------------------------------------------------------------------
# Anomalia 3 — Bets recém-adicionadas sem email (oportunidades de prospecção)
# ---------------------------------------------------------------------------

def novas_sem_email(registros: list[dict], dias: int = 30, limite: int = 15) -> list[dict]:
    """
    Retorna bets sem `email_contato` adicionadas nos últimos N dias
    (campo `_adicionado_em` se existir, senão `data_coleta`).

    Ordenado por data DESC (mais recente primeiro).
    """
    agora = datetime.now()
    cutoff = agora - timedelta(days=dias)

    anomalias: list[dict] = []
    for r in registros:
        if r.get("email_contato"):
            continue
        # Pula bets já com score alto (já têm ranking próprio na home)
        ts_str = r.get("_adicionado_em") or r.get("data_coleta") or ""
        ts = _parse_iso(ts_str)
        if not ts or ts < cutoff:
            continue
        anomalias.append({
            "marca":         r.get("marca", ""),
            "cnpj":          r.get("cnpj", ""),
            "url":           r.get("url", ""),
            "uf":            r.get("uf", ""),
            "municipio":     r.get("municipio", ""),
            "porte":         r.get("porte_empresa", ""),
            "adicionado_em": ts.isoformat(timespec="seconds"),
            "dias_atras":    (agora - ts).days,
            "health_score":  r.get("_health_score"),
            "ra_status":     r.get("_ra_status", "desconhecido"),
        })
    anomalias.sort(key=lambda a: a["dias_atras"])  # mais recente primeiro
    return anomalias[:limite]


# ---------------------------------------------------------------------------
# Resumo consolidado (consumido por /api/anomalias)
# ---------------------------------------------------------------------------

def resumo(registros: list[dict]) -> dict:
    """Retorna as 3 listas + contadores de cada categoria."""
    caindo = urls_caindo(registros)
    queda  = queda_ra(registros)
    novas  = novas_sem_email(registros)
    return {
        "urls_caindo": {
            "total": len(caindo),
            "itens": caindo[:10],
        },
        "queda_ra": {
            "total": len(queda),
            "itens": queda[:10],
        },
        "novas_sem_email": {
            "total": len(novas),
            "itens": novas,
        },
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
    }
