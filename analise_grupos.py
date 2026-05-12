"""
analise_grupos.py — Agrupamento de bets por holding empresarial (CNPJ raiz)
============================================================================
CNPJ tem o formato: XX.XXX.XXX/0001-XX (14 dígitos).
Os **8 primeiros dígitos** identificam a empresa-matriz; os 4 seguintes (filial)
e os 2 últimos (DV) variam entre estabelecimentos do mesmo grupo.

Bets diferentes com o mesmo CNPJ raiz pertencem ao mesmo grupo empresarial.

Exemplo concreto observado nos dados:
    BPX BETS SPORTS GROUP LTDA (CNPJ raiz 55.590.815) opera:
        VAIDEBET   (CNPJ 55.590.815/0001-60)
        BETPIX365  (CNPJ 55.590.815/0001-60)
        OBABET     (CNPJ 55.590.815/0001-60)

API pública:
    cnpj_raiz(cnpj)            -> str de 8 dígitos
    agrupar(registros)         -> list[dict] com info por holding (ordenado por nº de marcas)
    holdings_top(registros, n) -> top N holdings com mais marcas
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


def cnpj_raiz(cnpj: str | None) -> str:
    """
    Retorna os 8 primeiros dígitos do CNPJ (identificador da matriz).
    Retorna string vazia se CNPJ inválido.
    """
    if not cnpj:
        return ""
    so_digitos = re.sub(r"\D", "", str(cnpj))
    if len(so_digitos) < 8:
        return ""
    return so_digitos[:8]


def _media(valores: list) -> float:
    """Média ignorando None."""
    nums = [v for v in valores if v is not None]
    return round(sum(nums) / len(nums), 1) if nums else 0.0


def agrupar(registros: list[dict]) -> list[dict]:
    """
    Agrupa registros por CNPJ raiz e retorna lista de holdings.
    Cada holding tem:
        - cnpj_raiz:       8 dígitos
        - razao_social:    nome legal (do primeiro registro)
        - marcas:          list[str] de marcas
        - n_marcas:        int
        - score_medio:     média do _health_score
        - ra_nota_media:   média da _ra_nota (None se nenhuma encontrada)
        - n_ra1000:        contagem com selo RA1000
        - n_online:        contagem com URL acessível (ok/redirect/bloqueado)
        - n_inativas:      contagem com URL inativa
        - n_com_email:     contagem com email_contato
        - score_min:       pior score do grupo
        - score_max:       melhor score do grupo
        - registros:       list[dict] dos registros originais (subset essencial)

    Ordenação: por n_marcas DESC, depois por score_medio DESC.
    Apenas grupos com ≥ 2 marcas são retornados (holdings reais).
    """
    grupos: dict[str, list[dict]] = defaultdict(list)
    for r in registros:
        raiz = cnpj_raiz(r.get("cnpj"))
        if raiz:
            grupos[raiz].append(r)

    holdings: list[dict] = []
    _ONLINE = {"ok", "redirect", "bloqueado"}
    for raiz, regs in grupos.items():
        if len(regs) < 2:
            continue
        marcas = sorted({(r.get("marca") or "").strip() for r in regs if r.get("marca")})
        scores = [r.get("_health_score") for r in regs]
        ra_notas = [r.get("_ra_nota") for r in regs if r.get("_ra_nota") is not None]

        holding = {
            "cnpj_raiz":     raiz,
            "razao_social":  (regs[0].get("razao_social") or "").strip(),
            "marcas":        marcas,
            "n_marcas":      len(marcas),
            "score_medio":   _media(scores),
            "score_min":     min((s for s in scores if s is not None), default=0),
            "score_max":     max((s for s in scores if s is not None), default=0),
            "ra_nota_media": _media(ra_notas),
            "n_ra1000":      sum(1 for r in regs if r.get("_ra_ra1000")),
            "n_online":      sum(1 for r in regs if (r.get("_url_health_status") or "") in _ONLINE),
            "n_inativas":    sum(1 for r in regs if r.get("_url_inativa")),
            "n_com_email":   sum(1 for r in regs if r.get("email_contato")),
            "uf":            (regs[0].get("uf") or ""),
            "municipio":     (regs[0].get("municipio") or ""),
        }
        # Resumo dos registros (campos essenciais para render no front)
        holding["registros"] = [{
            "marca":               r.get("marca"),
            "cnpj":                r.get("cnpj"),
            "url":                 r.get("url"),
            "email_contato":       r.get("email_contato"),
            "_health_score":       r.get("_health_score"),
            "_health_score_classe": r.get("_health_score_classe"),
            "_ra_nota":            r.get("_ra_nota"),
            "_ra_reputacao":       r.get("_ra_reputacao"),
            "_ra_ra1000":          r.get("_ra_ra1000"),
            "_url_health_status":  r.get("_url_health_status"),
            "_url_inativa":        r.get("_url_inativa"),
        } for r in regs]
        holdings.append(holding)

    holdings.sort(key=lambda h: (-h["n_marcas"], -h["score_medio"]))
    return holdings


def holdings_top(registros: list[dict], n: int = 5) -> list[dict]:
    """Retorna as N maiores holdings (por n_marcas)."""
    return agrupar(registros)[:n]


def estatisticas_grupos(registros: list[dict]) -> dict:
    """
    Retorna stats agregadas sobre holdings:
        total_grupos:        nº de grupos com ≥2 marcas
        total_marcas_em_grupos: nº de marcas dentro de algum grupo
        maior_grupo:         dict com info do maior
        marcas_independentes: nº de marcas que não estão em nenhum grupo
    """
    holdings = agrupar(registros)
    marcas_em_grupos = sum(h["n_marcas"] for h in holdings)
    total_com_cnpj = sum(1 for r in registros if cnpj_raiz(r.get("cnpj")))

    return {
        "total_grupos":           len(holdings),
        "total_marcas_em_grupos": marcas_em_grupos,
        "marcas_independentes":   max(0, total_com_cnpj - marcas_em_grupos),
        "maior_grupo":            holdings[0] if holdings else None,
    }
