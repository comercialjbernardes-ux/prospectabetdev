"""
health_score.py — Score composto de saúde/qualificação por bet (0-100)
========================================================================
Combina 5 dimensões já coletadas pelo sistema em um índice único 0-100,
útil para ranking, filtragem e detecção de degradação ao longo do tempo.

Composição (pesos totalizando 100%):
    URL health         25%   (ok=100, redirect=80, bloqueado=70, desconhecido=50, erro=0)
    Nota Reclame Aqui  25%   (nota × 10; RA1000 +5 bonus; sem dado = 50 neutro)
    % resolvidas RA    20%   (percentual direto; sem dado = 50 neutro)
    Afiliados          15%   (sim=100, nao_encontrado=50, nao=20)
    Email encontrado   15%   (tem=100, sem=0)

API pública:
    calcular(registro)      -> int (0-100)
    classificar(score)      -> "excelente" | "bom" | "regular" | "ruim" | "critico"
    aplicar_em_lote(lista)  -> None  (escreve campo '_health_score' em cada registro)

Exemplos de score esperados:
    BETANO (RA1000 9.0, URL ok, com email, sem afiliados)  → ~88
    SPORTINGBET (Regular 6.7, URL ok, sem email, com afil) → ~62
    Bet inativa sem email, sem RA, sem afil                → ~25
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Pesos da composição (devem somar 1.0)
# ---------------------------------------------------------------------------

PESO_URL:        float = 0.25
PESO_NOTA_RA:    float = 0.25
PESO_RESOL_RA:   float = 0.20
PESO_AFILIADOS:  float = 0.15
PESO_EMAIL:      float = 0.15

_VALOR_NEUTRO: float = 50.0   # usado quando um campo não tem dado disponível


# ---------------------------------------------------------------------------
# Sub-scores por dimensão (0-100 cada)
# ---------------------------------------------------------------------------

def _score_url(reg: dict) -> float:
    status = (reg.get("_url_health_status") or "desconhecido").lower()
    if reg.get("_url_inativa"):
        return 0.0
    return {
        "ok":           100.0,
        "redirect":      80.0,
        "bloqueado":     70.0,   # site ativo, bloqueia bots
        "desconhecido":  _VALOR_NEUTRO,
        "erro_http":      0.0,
        "erro_conexao":   0.0,
        "erro_ssl":       0.0,
        "erro_dns":       0.0,
        "timeout":        0.0,
        "erro":           0.0,
    }.get(status, _VALOR_NEUTRO)


def _score_nota_ra(reg: dict) -> float:
    nota = reg.get("_ra_nota")
    # Sem dado → neutro
    if nota is None:
        if reg.get("_ra_status") == "nao_encontrado":
            return 35.0    # ligeiramente abaixo do neutro: estar fora do RA é levemente pior
        return _VALOR_NEUTRO
    try:
        val = float(nota) * 10.0   # nota 0-10 → 0-100
    except (TypeError, ValueError):
        return _VALOR_NEUTRO
    # Bonus RA1000: selo premium do Reclame Aqui
    if reg.get("_ra_ra1000"):
        val = min(100.0, val + 5.0)
    return max(0.0, min(100.0, val))


def _score_resol_ra(reg: dict) -> float:
    resol = reg.get("_ra_resolvidas")
    if resol is None:
        if reg.get("_ra_status") == "nao_encontrado":
            return _VALOR_NEUTRO
        return _VALOR_NEUTRO
    try:
        return max(0.0, min(100.0, float(resol)))
    except (TypeError, ValueError):
        return _VALOR_NEUTRO


def _score_afiliados(reg: dict) -> float:
    disp = (reg.get("_afiliados_display") or "nao_encontrado").lower()
    return {
        "sim":             100.0,
        "nao_encontrado":  _VALOR_NEUTRO,   # ainda não checado
        "nao":              20.0,           # checado e não tem
    }.get(disp, _VALOR_NEUTRO)


def _score_email(reg: dict) -> float:
    email = (reg.get("email_contato") or "").strip()
    return 100.0 if email else 0.0


# ---------------------------------------------------------------------------
# Score composto
# ---------------------------------------------------------------------------

def calcular(reg: dict) -> int:
    """
    Calcula o score 0-100 de um registro de bet.
    Retorna int arredondado.
    """
    bruto = (
        _score_url(reg)        * PESO_URL +
        _score_nota_ra(reg)    * PESO_NOTA_RA +
        _score_resol_ra(reg)   * PESO_RESOL_RA +
        _score_afiliados(reg)  * PESO_AFILIADOS +
        _score_email(reg)      * PESO_EMAIL
    )
    return int(round(max(0.0, min(100.0, bruto))))


def detalhe(reg: dict) -> dict:
    """
    Retorna decomposição do score: cada componente + score final.
    Útil para tooltip explicativo no frontend.
    """
    su  = _score_url(reg)
    sn  = _score_nota_ra(reg)
    sr  = _score_resol_ra(reg)
    sa  = _score_afiliados(reg)
    se  = _score_email(reg)
    total = calcular(reg)
    return {
        "score":             total,
        "classificacao":     classificar(total),
        "componentes": {
            "url":         {"valor": round(su),  "peso": PESO_URL},
            "nota_ra":     {"valor": round(sn),  "peso": PESO_NOTA_RA},
            "resolvidas":  {"valor": round(sr),  "peso": PESO_RESOL_RA},
            "afiliados":   {"valor": round(sa),  "peso": PESO_AFILIADOS},
            "email":       {"valor": round(se),  "peso": PESO_EMAIL},
        },
    }


def classificar(score: int) -> str:
    """Mapeia score numérico para classe textual usada no badge frontend."""
    if score >= 80: return "excelente"
    if score >= 65: return "bom"
    if score >= 50: return "regular"
    if score >= 30: return "ruim"
    return "critico"


# ---------------------------------------------------------------------------
# Aplicação em lote (escreve _health_score e _health_score_classe in-place)
# ---------------------------------------------------------------------------

def aplicar_em_lote(registros: list[dict]) -> None:
    """Para cada registro, calcula e escreve `_health_score` e `_health_score_classe`."""
    for r in registros:
        s = calcular(r)
        r["_health_score"]        = s
        r["_health_score_classe"] = classificar(s)


def estatisticas(registros: list[dict]) -> dict:
    """
    Retorna estatísticas agregadas dos scores: media, mediana, percentis e
    contagem por classificação. Usado por stats_snapshot e dashboard.
    """
    if not registros:
        return {
            "media":      0,
            "mediana":    0,
            "min":        0,
            "max":        0,
            "por_classe": {"excelente": 0, "bom": 0, "regular": 0, "ruim": 0, "critico": 0},
        }
    scores = sorted(int(r.get("_health_score") or calcular(r)) for r in registros)
    media   = sum(scores) / len(scores)
    mediana = scores[len(scores) // 2]
    por_classe: dict[str, int] = {"excelente": 0, "bom": 0, "regular": 0, "ruim": 0, "critico": 0}
    for s in scores:
        por_classe[classificar(s)] += 1
    return {
        "media":      round(media, 1),
        "mediana":    mediana,
        "min":        scores[0],
        "max":        scores[-1],
        "por_classe": por_classe,
    }
