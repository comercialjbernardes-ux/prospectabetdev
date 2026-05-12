"""
enriquecer_base.py — Enriquecimento em lote de bets_enriquecidas.json
======================================================================
Consulta BrasilAPI (e ReceitaWS como fallback) para popular os campos
cadastrais ausentes (uf, municipio, porte_empresa, situacao_cadastral,
capital_social, regime_tributario, etc.) nos registros do JSON base.

Uso:
    python enriquecer_base.py                # enriquece todos sem uf/municipio
    python enriquecer_base.py --force        # reenriquece todos os registros
    python enriquecer_base.py --limite 10    # testa com os 10 primeiros

Importação programática (usada por csv_sync.py e app.py):
    from enriquecer_base import enriquecer_registros_pendentes
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

from enriquecer_cnpj import enriquecer_empresa, limpar_cnpj

logger = logging.getLogger("enriquecer_base")

ARQUIVO_JSON = Path("dados/bets_enriquecidas.json")
MAX_WORKERS = 3  # BrasilAPI aguenta, ReceitaWS tem rate-limit 3/min

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------


def _carregar_base() -> list[dict]:
    if not ARQUIVO_JSON.exists():
        return []
    try:
        with open(ARQUIVO_JSON, encoding="utf-8") as f:
            return json.load(f) or []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Falha ao carregar JSON: {e}")
        return []


def _salvar_base(dados: list[dict]) -> None:
    ARQUIVO_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARQUIVO_JSON.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        tmp.replace(ARQUIVO_JSON)
    except OSError as e:
        logger.error(f"Falha ao salvar JSON: {e}")


# ---------------------------------------------------------------------------
# Worker por CNPJ (com cache: várias marcas no mesmo CNPJ = 1 consulta)
# ---------------------------------------------------------------------------


def _worker(cnpj: str, marcas: list[str], idx: int, total: int) -> tuple[str, dict]:
    """Consulta APIs para um CNPJ e retorna (cnpj_limpo, dados_enriquecidos)."""
    cnpj_limpo = limpar_cnpj(cnpj)
    logger.info(f"[{idx}/{total}] CNPJ {cnpj} — {marcas[:2]}")
    sessao = requests.Session()
    dados = enriquecer_empresa(cnpj, "", sessao)
    try:
        sessao.close()
    except Exception:
        pass
    return cnpj_limpo, dados


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def enriquecer_registros_pendentes(force: bool = False, limite: int | None = None) -> dict:
    """
    Enriquece registros sem uf/municipio no JSON base.

    Parâmetros:
        force   — se True, reenriquece mesmo quem já tem uf/municipio
        limite  — máximo de CNPJs únicos a processar nesta chamada

    Retorna dict com estatísticas: {"processados": N, "atualizados": N, "erros": N}
    """
    with _lock:
        dados = _carregar_base()
        if not dados:
            return {"processados": 0, "atualizados": 0, "erros": 0}

        # Agrupa por CNPJ limpo — evita duplicar consultas para empresas com múltiplas marcas
        cnpj_para_registros: dict[str, list[dict]] = {}
        for r in dados:
            cnpj_limpo = limpar_cnpj(r.get("cnpj") or "")
            if not cnpj_limpo or len(cnpj_limpo) != 14:
                continue
            # Pula quem já tem UF — a menos que force=True
            if not force and r.get("uf"):
                continue
            cnpj_para_registros.setdefault(cnpj_limpo, []).append(r)

        if not cnpj_para_registros:
            logger.info("Nenhum registro pendente de enriquecimento.")
            return {"processados": 0, "atualizados": 0, "erros": 0}

        cnpjs_pendentes = list(cnpj_para_registros.items())
        if limite:
            cnpjs_pendentes = cnpjs_pendentes[:limite]

        total = len(cnpjs_pendentes)
        logger.info(
            f"Enriquecimento: {total} CNPJs únicos "
            f"({'forçado' if force else 'pendentes'})"
        )

        resultados: dict[str, dict] = {}
        erros = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futuros = {
                executor.submit(
                    _worker,
                    cnpj_raw,  # mantém máscara para display nos logs
                    [r.get("marca", "") for r in registros],
                    i + 1,
                    total,
                ): cnpj_raw
                for i, (cnpj_raw, registros) in enumerate(cnpjs_pendentes)
                # cnpj_raw é o cnpj_limpo que já usamos como chave
            }
            for futuro in as_completed(futuros):
                try:
                    cnpj_limpo_ret, dados_cnpj = futuro.result()
                    resultados[cnpj_limpo_ret] = dados_cnpj
                except Exception as e:
                    erros += 1
                    logger.error(f"Erro em worker CNPJ: {e}")

        # Aplica os resultados de volta nos registros (muta in-place)
        atualizados = 0
        ts = datetime.now().isoformat(timespec="seconds")
        for r in dados:
            cnpj_limpo = limpar_cnpj(r.get("cnpj") or "")
            if cnpj_limpo not in resultados:
                continue
            enriched = resultados[cnpj_limpo]
            # Só sobrescreve campos que vieram com dados reais
            # (não sobrescreve com strings vazias se já tínhamos algo)
            houve_mudanca = False
            for campo, valor in enriched.items():
                if campo.startswith("_"):
                    continue
                if valor or not r.get(campo):
                    if r.get(campo) != valor:
                        r[campo] = valor
                        houve_mudanca = True
            if houve_mudanca:
                r["_enriquecido_em"] = ts
                atualizados += 1

        if atualizados:
            _salvar_base(dados)
            logger.info(f"JSON atualizado: {atualizados} registros enriquecidos.")
        else:
            logger.info("Nenhum registro precisou de atualização.")

        return {
            "processados": total,
            "atualizados": atualizados,
            "erros": erros,
        }


# ---------------------------------------------------------------------------
# CLI standalone
# ---------------------------------------------------------------------------


def _configurar_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    _configurar_logging()
    parser = argparse.ArgumentParser(
        description="Enriquece bets_enriquecidas.json com dados de CNPJ.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Reenriquece mesmo registros que já têm uf/municipio.",
    )
    parser.add_argument(
        "--limite", type=int, metavar="N",
        help="Processa no máximo N CNPJs únicos.",
    )
    args = parser.parse_args()

    inicio = time.time()
    resultado = enriquecer_registros_pendentes(force=args.force, limite=args.limite)
    elapsed = time.time() - inicio

    print(
        f"\nConcluído em {elapsed:.1f}s — "
        f"processados: {resultado['processados']} | "
        f"atualizados: {resultado['atualizados']} | "
        f"erros: {resultado['erros']}"
    )


if __name__ == "__main__":
    main()
