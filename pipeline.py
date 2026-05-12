"""
pipeline.py — Orquestrador unificado: coleta emails + enriquece CNPJ
=====================================================================
Uso:
    python pipeline.py                     # coleta emails e enriquece CNPJ
    python pipeline.py --limite 10         # testa com as 10 primeiras empresas
    python pipeline.py --so-cnpj           # só enriquece CNPJ (usa CSV existente)
    python pipeline.py --reiniciar         # zera checkpoint e reprocessa tudo
    python pipeline.py --csv arquivo.csv   # usa CSV local

Saídas:
    dados/bets_enriquecidas.json  — cache para o dashboard Flask
    bets_com_emails.csv           — CSV legado com emails
    relatorio.txt                 — resumo da coleta
    checkpoint.json               — progresso salvo (permite retomada)
    coleta.log                    — log completo
"""

import argparse
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import urllib3

from coletar_afiliados import coletar_afiliados
from coletar_bets import (
    URL_CSV_OFICIAL,
    carregar_checkpoint,
    carregar_dataframe,
    coletar_email_empresa,
    configurar_logging,
    exportar_csv,
    gerar_relatorio,
    salvar_checkpoint,
)
from enriquecer_cnpj import enriquecer_empresa
from validar_regime import calcular_confiabilidade, validar_regime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("coletar_bets")

ARQUIVO_JSON = Path("dados/bets_enriquecidas.json")

# Workers para email: até 5 simultâneos (IO-bound, sites distintos)
MAX_WORKERS_EMAIL = 5
# Workers para CNPJ: máximo 3 (APIs públicas têm rate limit)
MAX_WORKERS_CNPJ = 3
# Workers para afiliados: 4 (IO-bound, mais pesado que email por ter 2 downloads)
MAX_WORKERS_AFILIADOS = 4

_CHECKPOINT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Workers individuais
# ---------------------------------------------------------------------------


def _worker_email(row, index: int, total: int) -> dict:
    """Coleta email de uma empresa. Cada worker usa sua própria Session."""
    url = str(getattr(row, "url", "")).strip()
    cnpj = str(getattr(row, "cnpj", "")).strip()
    marca = str(getattr(row, "marca", "")).strip()
    razao_social = str(getattr(row, "razao_social", "")).strip()

    sessao = requests.Session()
    logger.info(f"[{index}/{total}] Email ▶ {marca} ({url})")

    email, status = coletar_email_empresa(url, sessao)
    time.sleep(random.uniform(1.0, 3.0))

    return {
        "marca": marca,
        "razao_social": razao_social,
        "cnpj": cnpj,
        "url": url,
        "email_contato": email,
        "status": status,
        "data_coleta": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _worker_cnpj(registro: dict, index: int, total: int) -> dict:
    """Enriquece dados CNPJ de uma empresa. Cada worker usa sua própria Session."""
    cnpj = registro.get("cnpj", "")
    url = registro.get("url", "")
    marca = registro.get("marca", "")

    sessao = requests.Session()
    logger.info(f"[{index}/{total}] CNPJ ▶ {marca} ({cnpj})")

    dados_cnpj = enriquecer_empresa(cnpj, url, sessao)

    # Revalida e recalcula confiabilidade após enriquecimento
    regime_corrigido, _ = validar_regime(
        dados_cnpj["regime_tributario"],
        dados_cnpj["capital_social"],
        dados_cnpj["porte_empresa"],
    )
    dados_cnpj["regime_tributario"] = regime_corrigido
    dados_cnpj["confiabilidade_dado"] = calcular_confiabilidade(
        dados_cnpj["fonte_regime"],
        regime_corrigido,
        dados_cnpj["capital_social"],
        dados_cnpj["porte_empresa"],
    )

    return {**registro, **dados_cnpj}


# ---------------------------------------------------------------------------
# Coleta de emails em paralelo
# ---------------------------------------------------------------------------


def coletar_emails_paralelo(
    df, checkpoint: dict, limite: int | None
) -> list[dict]:
    """
    Coleta emails com ThreadPoolExecutor.
    Registros já presentes no checkpoint são pulados e incluídos diretamente.
    """
    total = min(len(df), limite) if limite else len(df)
    resultados: list[dict] = []
    resultados_lock = threading.Lock()

    # Separa registros já processados (checkpoint) dos pendentes
    pendentes = []
    for i, row in enumerate(df.itertuples()):
        if limite and i >= limite:
            break
        url = str(getattr(row, "url", "")).strip()
        chave = url or str(getattr(row, "cnpj", i))
        if chave in checkpoint:
            with resultados_lock:
                resultados.append(checkpoint[chave])
        else:
            pendentes.append((row, i + 1, total))

    if not pendentes:
        logger.info("Todos os registros já estão no checkpoint.")
        return resultados

    logger.info(
        f"Coletando emails: {len(pendentes)} pendentes, "
        f"{total - len(pendentes)} já no checkpoint."
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_EMAIL) as executor:
        futuros = {
            executor.submit(_worker_email, row, idx, tot): (row, idx)
            for row, idx, tot in pendentes
        }
        for futuro in as_completed(futuros):
            try:
                registro = futuro.result()
                chave = registro["url"] or registro["cnpj"]
                with resultados_lock:
                    resultados.append(registro)
                with _CHECKPOINT_LOCK:
                    checkpoint[chave] = registro
                    if len(resultados) % 5 == 0:
                        salvar_checkpoint(checkpoint)
            except Exception as e:
                logger.error(f"Erro no worker de email: {e}")

    salvar_checkpoint(checkpoint)
    return resultados


# ---------------------------------------------------------------------------
# Enriquecimento CNPJ em paralelo
# ---------------------------------------------------------------------------


def enriquecer_cnpj_paralelo(registros: list[dict]) -> list[dict]:
    """
    Enriquece dados CNPJ com ThreadPoolExecutor (máx. 3 workers).
    Usa cache interno em enriquecer_cnpj.py para empresas com múltiplas marcas.
    """
    total = len(registros)
    enriquecidos: list[dict] = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_CNPJ) as executor:
        futuros = {
            executor.submit(_worker_cnpj, reg, i + 1, total): reg
            for i, reg in enumerate(registros)
        }
        for futuro in as_completed(futuros):
            try:
                resultado = futuro.result()
                with lock:
                    enriquecidos.append(resultado)
            except Exception as e:
                logger.error(f"Erro no worker de CNPJ: {e}")

    # Mantém ordem original por marca
    ordem = {r["url"]: i for i, r in enumerate(registros)}
    enriquecidos.sort(key=lambda r: ordem.get(r.get("url", ""), 9999))
    return enriquecidos


# ---------------------------------------------------------------------------
# Coleta de afiliados em paralelo
# ---------------------------------------------------------------------------


def _worker_afiliados(registro: dict, index: int, total: int) -> dict:
    """Coleta link/email do programa de afiliados. Session própria por worker."""
    url = registro.get("url", "")
    marca = registro.get("marca", "")

    sessao = requests.Session()
    logger.info(f"[{index}/{total}] Afiliados ▶ {marca} ({url})")

    try:
        url_af, _email_descartado, status_af = coletar_afiliados(url, sessao)
    except Exception as e:
        logger.error(f"Erro coletando afiliados para {marca}: {e}")
        url_af, status_af = "", "erro_conexao"

    time.sleep(random.uniform(0.8, 2.0))

    # Apenas URL é persistida — email de afiliados foi removido do escopo do produto
    return {
        **registro,
        "url_afiliados": url_af,
        "status_afiliados": status_af,
    }


def coletar_afiliados_paralelo(registros: list[dict]) -> list[dict]:
    """Roda coleta de afiliados em paralelo sobre registros existentes."""
    total = len(registros)
    resultados: list[dict] = []
    lock = threading.Lock()

    # Pula quem já tem afiliados coletados (permite retomar sem reprocessar)
    pendentes = [
        (i + 1, reg) for i, reg in enumerate(registros)
        if not reg.get("status_afiliados")
    ]
    ja_processados = [
        reg for reg in registros if reg.get("status_afiliados")
    ]
    resultados.extend(ja_processados)

    if not pendentes:
        logger.info("Todos os registros já têm afiliados coletados.")
        return resultados

    logger.info(
        f"Coletando afiliados: {len(pendentes)} pendentes, "
        f"{len(ja_processados)} já processados."
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_AFILIADOS) as executor:
        futuros = {
            executor.submit(_worker_afiliados, reg, idx, total): reg
            for idx, reg in pendentes
        }
        for futuro in as_completed(futuros):
            try:
                resultado = futuro.result()
                with lock:
                    resultados.append(resultado)
            except Exception as e:
                logger.error(f"Erro no worker de afiliados: {e}")

    # Mantém ordem original
    ordem = {r.get("cnpj", ""): i for i, r in enumerate(registros)}
    resultados.sort(key=lambda r: ordem.get(r.get("cnpj", ""), 9999))
    return resultados


# ---------------------------------------------------------------------------
# Persistência do JSON para o dashboard
# ---------------------------------------------------------------------------


def salvar_json(dados: list[dict]) -> None:
    """Salva dados enriquecidos em JSON para consumo do dashboard Flask."""
    ARQUIVO_JSON.parent.mkdir(exist_ok=True)
    with open(ARQUIVO_JSON, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"JSON salvo em: {ARQUIVO_JSON} ({len(dados)} registros)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline unificado: coleta emails + enriquece CNPJ.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--csv", metavar="ARQUIVO",
                        help="CSV local do gov.br (omitir para baixar automaticamente).")
    parser.add_argument("--limite", type=int, metavar="N",
                        help="Processa apenas as primeiras N empresas.")
    parser.add_argument("--reiniciar", action="store_true",
                        help="Ignora checkpoint e reprocessa tudo.")
    parser.add_argument("--so-cnpj", action="store_true",
                        help="Pula coleta de email; só enriquece CNPJ usando CSV existente.")
    parser.add_argument("--so-afiliados", action="store_true",
                        help="Carrega JSON existente e só coleta afiliados (URL do programa).")
    parser.add_argument("--com-afiliados", action="store_true",
                        help="Inclui a etapa de afiliados no pipeline completo (desligada por padrão).")
    return parser.parse_args()


def main() -> None:
    configurar_logging()
    args = parse_args()

    logger.info("=" * 60)
    logger.info("  PIPELINE — BETS LEGALIZADAS BR")
    logger.info("=" * 60)

    checkpoint = {} if args.reiniciar else carregar_checkpoint()
    if args.reiniciar:
        logger.info("Modo --reiniciar: checkpoint zerado.")

    # Modo --so-afiliados: pula tudo exceto coleta de afiliados sobre JSON existente
    if args.so_afiliados:
        if not ARQUIVO_JSON.exists():
            logger.error(
                f"{ARQUIVO_JSON} não existe. Rode o pipeline completo antes de --so-afiliados."
            )
            return
        with open(ARQUIVO_JSON, encoding="utf-8") as f:
            registros = json.load(f)
        if args.limite:
            registros = registros[: args.limite]
        logger.info(f"Modo --so-afiliados: {len(registros)} registros carregados do JSON.")
        registros_com_afiliados = coletar_afiliados_paralelo(registros)
        salvar_json(registros_com_afiliados)
        com_af = sum(
            1 for r in registros_com_afiliados
            if r.get("status_afiliados", "").startswith("encontrado")
        )
        logger.info(
            f"Afiliados concluído: {com_af}/{len(registros_com_afiliados)} com dados coletados."
        )
        return

    if args.so_cnpj:
        # Carrega registros já coletados do CSV legado
        csv_path = Path("bets_com_emails.csv")
        if not csv_path.exists():
            logger.error(
                "bets_com_emails.csv não encontrado. "
                "Execute sem --so-cnpj primeiro para coletar emails."
            )
            return
        import pandas as pd
        base = pd.read_csv(csv_path, dtype=str).fillna("")
        if args.limite:
            base = base.head(args.limite)
        registros = base.to_dict("records")
        logger.info(f"Modo --so-cnpj: {len(registros)} registros carregados do CSV.")
    else:
        # Etapa 1: carrega CSV oficial
        df = carregar_dataframe(args.csv, URL_CSV_OFICIAL)
        # Etapa 2: coleta emails em paralelo
        registros = coletar_emails_paralelo(df, checkpoint, args.limite)
        # Exporta CSV legado e relatório
        exportar_csv(registros)
        gerar_relatorio(registros)

    # Etapa 3: enriquece CNPJ em paralelo
    logger.info(f"Iniciando enriquecimento CNPJ para {len(registros)} registros...")
    registros_enriquecidos = enriquecer_cnpj_paralelo(registros)

    # Etapa 4: coleta afiliados — DESLIGADA por padrão (links são adicionados
    # manualmente via dashboard). Rode `python pipeline.py --com-afiliados`
    # ou `--so-afiliados` se quiser reativar a coleta automática.
    if args.com_afiliados:
        logger.info(f"Iniciando coleta de afiliados para {len(registros_enriquecidos)} registros...")
        registros_enriquecidos = coletar_afiliados_paralelo(registros_enriquecidos)

    # Salva JSON para o dashboard
    salvar_json(registros_enriquecidos)

    # Estatísticas rápidas
    regime_ids = sum(
        1 for r in registros_enriquecidos
        if r.get("regime_tributario") not in ("Não identificado", "", None)
    )
    logger.info(
        f"Pipeline concluído: {len(registros_enriquecidos)} registros | "
        f"{regime_ids} com regime identificado."
    )


if __name__ == "__main__":
    main()
