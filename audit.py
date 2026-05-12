"""
audit.py — Sistema de auditoria de edições (append-only, JSONL)
================================================================
Mantém `dados/audit_log.jsonl` com cada edição manual feita via `/api/editar`,
incluindo rotação automática quando o arquivo cresce demais.

API pública:
    registrar(acao, cnpj, campo, valor_anterior, valor_novo, ip="")
    rotacionar()                        — checa e trunca se necessário
    ler_tudo() -> list[dict]            — lê todas as entradas (mais recente primeiro)
    ler_paginado(filtros, pagina, por_pagina, cnpj_marca_map) -> (eventos, total)

Schema de cada entrada (linha JSON):
    {
        "ts": "2026-05-12T19:30:00",
        "acao": "edit" | "delete" | "reset",
        "cnpj": "12.345.678/0001-00",
        "campo": "email_contato",
        "valor_anterior": str | None,
        "valor_novo":     str | None,
        "ip": "127.0.0.1"
    }

Constantes configuráveis:
    ARQUIVO_AUDIT       — caminho do arquivo (default dados/audit_log.jsonl)
    MAX_LINHAS          — limiar para disparar rotação (50.000)
    TRUNCAR_PARA        — quantas linhas manter ao rotacionar (40.000)
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

ARQUIVO_AUDIT: Path = Path("dados/audit_log.jsonl")
MAX_LINHAS:    int  = 50_000
TRUNCAR_PARA:  int  = 40_000

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------

def registrar(
    acao: str,
    cnpj: str,
    campo: str,
    valor_anterior: Any,
    valor_novo: Any,
    ip: str = "",
) -> None:
    """Acrescenta uma linha JSON ao audit log (append-only, thread-safe)."""
    entrada = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "acao": acao,
        "cnpj": cnpj,
        "campo": campo,
        "valor_anterior": valor_anterior,
        "valor_novo": valor_novo,
        "ip": ip,
    }
    ARQUIVO_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with open(ARQUIVO_AUDIT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entrada, ensure_ascii=False) + "\n")
    logger.info("audit", extra=entrada)
    # Rotaciona se atingiu limite
    rotacionar()


def rotacionar() -> None:
    """Trunca audit_log.jsonl para as N linhas mais recentes se exceder MAX_LINHAS."""
    if not ARQUIVO_AUDIT.exists():
        return
    with _LOCK:
        try:
            with open(ARQUIVO_AUDIT, encoding="utf-8") as fh:
                linhas = fh.readlines()
            if len(linhas) <= MAX_LINHAS:
                return
            linhas = linhas[-TRUNCAR_PARA:]
            tmp = ARQUIVO_AUDIT.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(linhas), encoding="utf-8")
            tmp.replace(ARQUIVO_AUDIT)
            logger.info(f"audit_log truncado: mantidas {len(linhas)} linhas mais recentes")
        except OSError:
            logger.exception("Falha ao rotacionar audit log")


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------

def ler_tudo() -> list[dict]:
    """Lê todas as entradas do audit log, mais recentes primeiro. Falha → []."""
    if not ARQUIVO_AUDIT.exists():
        return []
    eventos: list[dict] = []
    try:
        with open(ARQUIVO_AUDIT, encoding="utf-8") as fh:
            for linha in fh:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    eventos.append(json.loads(linha))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    eventos.reverse()
    return eventos


def _normalizar_evento(ev: dict, cnpj_marca_map: dict[str, str]) -> dict:
    """Converte schema JSONL para o schema esperado pelo template auditoria.html."""
    ts = ev.get("ts") or ev.get("timestamp", "")
    ts_fmt = ""
    if ts:
        try:
            d = datetime.fromisoformat(ts.replace("Z", ""))
            ts_fmt = d.strftime("%d/%m/%y %H:%M:%S")
        except (ValueError, TypeError):
            ts_fmt = ts

    cnpj_raw = ev.get("cnpj", "")
    cnpj_so_digitos = re.sub(r"\D", "", cnpj_raw or "")
    marca = cnpj_marca_map.get(cnpj_so_digitos, "")

    return {
        "timestamp":     ts,
        "timestamp_fmt": ts_fmt,
        "acao":          (ev.get("acao") or "EDIT").upper(),
        "cnpj":          cnpj_raw,
        "marca":         marca,
        "campo":         ev.get("campo", ""),
        "valor_anterior": ev.get("valor_anterior") if ev.get("valor_anterior") not in (None, "") else "",
        "valor_novo":     ev.get("valor_novo")     if ev.get("valor_novo")     not in (None, "") else "",
        "usuario":        ev.get("usuario") or "system",
        "ip":             ev.get("ip", ""),
    }


def _passa_filtros(ev: dict, filtros: dict[str, str]) -> bool:
    """Aplica os filtros do form de auditoria a um evento já normalizado."""
    if filtros.get("acao") and ev["acao"] != filtros["acao"].upper():
        return False
    if filtros.get("campo") and ev["campo"] != filtros["campo"]:
        return False
    dt_ini = filtros.get("data_inicio")
    if dt_ini and ev["timestamp"][:10] < dt_ini:
        return False
    dt_fim = filtros.get("data_fim")
    if dt_fim and ev["timestamp"][:10] > dt_fim:
        return False
    q = filtros.get("q")
    if q:
        q_low = q.lower()
        blob = " ".join(str(ev.get(k, "")) for k in
                        ("marca", "campo", "usuario", "valor_anterior", "valor_novo")).lower()
        if q_low not in blob:
            return False
    return True


def ler_paginado(
    filtros: dict[str, str],
    pagina: int = 1,
    por_pagina: int = 50,
    cnpj_marca_map: dict[str, str] | None = None,
) -> tuple[list[dict], int, list[str]]:
    """
    Lê o audit log aplicando filtros e paginação.

    :param filtros: dict com keys opcionais: q, acao, campo, data_inicio, data_fim
    :param pagina: número da página (1-based)
    :param por_pagina: quantidade por página (default 50)
    :param cnpj_marca_map: dict CNPJ-só-dígitos → marca, para enriquecer cada evento

    :returns: (eventos_da_pagina, total_eventos_filtrados, campos_disponiveis)
    """
    cnpj_marca_map = cnpj_marca_map or {}
    eventos = [_normalizar_evento(e, cnpj_marca_map) for e in ler_tudo()]
    filtrados = [e for e in eventos if _passa_filtros(e, filtros)]
    total = len(filtrados)
    inicio = max(0, (pagina - 1) * por_pagina)
    pagina_eventos = filtrados[inicio:inicio + por_pagina]
    campos_disponiveis = sorted({e["campo"] for e in eventos if e["campo"]})
    return pagina_eventos, total, campos_disponiveis
