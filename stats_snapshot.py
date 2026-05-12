"""
stats_snapshot.py — Registro diário de KPIs para sparklines históricas
======================================================================
Grava um snapshot por dia em dados/stats_snapshots.json.
O endpoint /api/snapshots expõe os últimos N dias para o frontend.

Uso (chamado internamente por app.py):
    import stats_snapshot
    stats_snapshot.registrar_snapshot_se_necessario(dados)

Leitura:
    snapshots = stats_snapshot.ler_snapshots()
    # → [{"data": "2025-05-01", "total": 212, "com_email": 140, ...}, ...]
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

# Garante imports locais mesmo quando chamado de outro cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ARQUIVO_SNAPSHOTS = Path("dados/stats_snapshots.json")

# Máximo de dias que guardamos
_MAX_SNAPSHOTS = 90

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calcular_stats(dados: list[dict]) -> dict:
    """Computa KPIs de uma lista de registros (mesma lógica de api_stats)."""
    total     = len(dados)
    com_email = sum(1 for r in dados if r.get("status") in
                    ("encontrado", "encontrado_js", "encontrado_manual"))
    sem_email = sum(1 for r in dados if r.get("status") == "nao_encontrado")
    com_afil  = sum(1 for r in dados if r.get("_afiliados_display") == "sim")
    editados  = sum(1 for r in dados if r.get("_editado_manualmente"))
    urls_ok   = sum(1 for r in dados if r.get("_url_health_status") == "ok")
    urls_ina  = sum(1 for r in dados if r.get("_url_inativa"))

    # Health Score histórico (etapa 3.4)
    scores = [int(r["_health_score"]) for r in dados if "_health_score" in r]
    if scores:
        score_medio    = round(sum(scores) / len(scores), 1)
        score_mediana  = sorted(scores)[len(scores) // 2]
        score_excelente = sum(1 for s in scores if s >= 80)
        score_critico   = sum(1 for s in scores if s < 30)
    else:
        score_medio = score_mediana = 0.0
        score_excelente = score_critico = 0

    return {
        "total":           total,
        "com_email":       com_email,
        "sem_email":       sem_email,
        "com_afiliados":   com_afil,
        "editados":        editados,
        "urls_ativas":     urls_ok,
        "urls_inativas":   urls_ina,
        "score_medio":     score_medio,
        "score_mediana":   score_mediana,
        "score_excelente": score_excelente,    # bets com score >= 80
        "score_critico":   score_critico,      # bets com score < 30
    }


def _ler_raw() -> list[dict]:
    """Lê arquivo de snapshots do disco; retorna lista (mais antigo primeiro)."""
    if not ARQUIVO_SNAPSHOTS.exists():
        return []
    try:
        return json.loads(ARQUIVO_SNAPSHOTS.read_text("utf-8"))
    except Exception:
        return []


def _salvar_raw(snapshots: list[dict]) -> None:
    """Salva lista de snapshots atomicamente."""
    ARQUIVO_SNAPSHOTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARQUIVO_SNAPSHOTS.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(ARQUIVO_SNAPSHOTS)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def registrar_snapshot_se_necessario(dados: list[dict]) -> bool:
    """
    Grava um snapshot diário se ainda não existe um para hoje.

    :param dados: lista de registros atualizada (já com merges aplicados)
    :returns: True se snapshot novo gravado, False se já existia para hoje.
    """
    hoje = date.today().isoformat()   # "YYYY-MM-DD"
    with _lock:
        existentes = _ler_raw()
        # Já tem snapshot de hoje?
        if existentes and existentes[-1].get("data") == hoje:
            return False
        stats = _calcular_stats(dados)
        novo = {"data": hoje, "ts": datetime.now().isoformat(timespec="seconds"), **stats}
        existentes.append(novo)
        # Mantém apenas os últimos _MAX_SNAPSHOTS dias
        if len(existentes) > _MAX_SNAPSHOTS:
            existentes = existentes[-_MAX_SNAPSHOTS:]
        _salvar_raw(existentes)
    return True


def ler_snapshots(ultimos: int = _MAX_SNAPSHOTS) -> list[dict]:
    """
    Retorna os últimos N snapshots diários (mais antigo primeiro).
    Usado pelo endpoint /api/snapshots.
    """
    with _lock:
        todos = _ler_raw()
    return todos[-ultimos:] if len(todos) > ultimos else todos


def snapshot_atual(dados: list[dict]) -> dict:
    """
    Retorna um snapshot calculado em memória (sem gravar no disco).
    Útil para comparar com o último salvo.
    """
    return {
        "data": date.today().isoformat(),
        "ts":   datetime.now().isoformat(timespec="seconds"),
        **_calcular_stats(dados),
    }
