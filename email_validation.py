"""
email_validation.py — Worker de validação de emails (etapa 7)
==============================================================
Valida emails cadastrados em 2 níveis:
  1. **Sintaxe** — formato RFC válido (parser do `email-validator`)
  2. **Deliverability** — consulta MX record do domínio (DNS)

Resultados:
    valid      → sintaxe OK + MX existe (badge verde)
    no_mx      → sintaxe OK mas domínio não tem MX (badge amarelo)
    invalid    → sintaxe inválida (badge vermelho)
    sem_email  → registro não tem email_contato (badge cinza/oculto)

Estratégia idêntica aos outros workers daemon:
- TICK_SEGUNDOS=180 (3min, validação DNS é leve)
- 5 emails por tick (DNS query barata)
- Re-check a cada 7 dias (deliverability raramente muda)
- Circuit breaker via worker_utils

Storage: `dados/email_validation.json` keyed por email (lower-cased).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

# Garante imports locais quando rodado standalone (mesmo padrão dos outros workers)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json_store
from logging_config import get_logger
from worker_utils import CircuitBreaker

logger = get_logger(__name__)
_circuit_breaker = CircuitBreaker("email_validation", logger=logger)

try:
    from email_validator import EmailNotValidError, validate_email
    _LIB_DISPONIVEL = True
except ImportError:
    _LIB_DISPONIVEL = False
    EmailNotValidError = Exception  # type: ignore

# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------

TICK_SEGUNDOS:      int = 180     # 3min entre ticks (validação leve)
EMAILS_POR_TICK:    int = 5
WORKERS:            int = 2
INTERVALO_RE_CHECK: int = 7 * 86400   # re-checa cada 7 dias
TIMEOUT_DNS:        int = 5       # segundos para consultar MX

ARQUIVO_DADOS  = Path("dados/bets_enriquecidas.json")
ARQUIVO_HEALTH = Path("dados/email_validation.json")

_lock = threading.Lock()
_thread_ref: threading.Thread | None = None


# ---------------------------------------------------------------------------
# I/O thread-safe
# ---------------------------------------------------------------------------

def _carregar_health() -> dict:
    return json_store.ler(ARQUIVO_HEALTH, default={}) or {}


def _salvar_health(data: dict) -> None:
    json_store.salvar(ARQUIVO_HEALTH, data)


def ler_health() -> dict:
    """Leitura thread-safe (consumida pelo data_manager + frontend)."""
    with _lock:
        return _carregar_health()


def _listar_emails() -> list[str]:
    """Lista emails únicos cadastrados (base + overrides.json), lowercase."""
    emails: set[str] = set()

    # Base
    try:
        base = json_store.ler(ARQUIVO_DADOS, default=[]) or []
        for r in base:
            e = (r.get("email_contato") or "").strip().lower()
            if e and "@" in e:
                emails.add(e)
    except Exception:
        pass

    # Overrides (onde a maioria dos emails fica — adicionados manualmente)
    try:
        ov_raw = json_store.ler(Path("dados/overrides.json"), default={}) or {}
        for chave, ov in ov_raw.items():
            if chave.startswith("_") or not isinstance(ov, dict):
                continue
            e = (ov.get("email_contato") or "").strip().lower()
            if e and "@" in e:
                emails.add(e)
    except Exception:
        pass

    return sorted(emails)


# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------

def _validar_email(email: str) -> dict:
    """
    Valida um email. Retorna dict com:
        status:      "valid" | "no_mx" | "invalid" | "erro"
        normalized:  email normalizado (se válido sintaticamente)
        domain:      domínio (se válido sintaticamente)
        razao:       explicação textual em pt-BR
        checado_em:  ISO timestamp
    """
    ts = datetime.now().isoformat(timespec="seconds")
    base = {"checado_em": ts, "email": email}
    if not _LIB_DISPONIVEL:
        return {**base, "status": "erro", "razao": "Lib email-validator ausente"}

    # 1) Sintaxe (sem DNS)
    try:
        v_syntax = validate_email(email, check_deliverability=False)
    except EmailNotValidError as e:
        return {**base, "status": "invalid", "razao": str(e)[:200]}

    normalized = v_syntax.normalized
    domain = v_syntax.domain

    # 2) Deliverability (DNS - MX record)
    try:
        validate_email(email, check_deliverability=True, dns_resolver=None)
        return {
            **base,
            "status":     "valid",
            "normalized": normalized,
            "domain":     domain,
            "razao":      "Sintaxe OK + domínio tem MX record",
        }
    except EmailNotValidError as e:
        msg = str(e)[:200]
        # Heurística: se a falha é "domain does not exist" / "no MX"
        if "mx" in msg.lower() or "domain" in msg.lower() or "dns" in msg.lower():
            return {
                **base,
                "status":     "no_mx",
                "normalized": normalized,
                "domain":     domain,
                "razao":      f"Sintaxe OK mas sem MX: {msg}",
            }
        return {
            **base,
            "status":     "invalid",
            "normalized": normalized,
            "domain":     domain,
            "razao":      msg,
        }
    except Exception as e:
        # Falha de rede/DNS: marca como "erro" (não pune email válido por flicker de rede)
        return {
            **base,
            "status":     "erro",
            "normalized": normalized,
            "domain":     domain,
            "razao":      f"DNS error: {str(e)[:150]}",
        }


# ---------------------------------------------------------------------------
# Seleção de fatia (prioriza nunca-checados e mais antigos)
# ---------------------------------------------------------------------------

def _selecionar_fatia(emails: list[str], health: dict, n: int) -> list[str]:
    agora = datetime.now()

    def _idade_seg(email: str) -> float:
        info = health.get(email)
        if not info or not info.get("checado_em"):
            return float("inf")
        try:
            t = datetime.fromisoformat(info["checado_em"])
            return (agora - t).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    candidatos = [
        (e, _idade_seg(e)) for e in emails
        if _idade_seg(e) == float("inf") or _idade_seg(e) >= INTERVALO_RE_CHECK
    ]
    candidatos.sort(key=lambda x: -x[1])   # mais antigo primeiro
    return [e for e, _ in candidatos[:n]]


# ---------------------------------------------------------------------------
# Tick e loop
# ---------------------------------------------------------------------------

def _tick() -> tuple[int, int]:
    """Roda um ciclo. Retorna (n_validados, n_validos)."""
    emails = _listar_emails()
    with _lock:
        health = _carregar_health()

    fatia = _selecionar_fatia(emails, health, EMAILS_POR_TICK)
    if not fatia:
        # Heartbeat: nenhum email para re-validar agora, mas toca o arquivo
        # para que o monitor de saúde saiba que o worker está ativo.
        with _lock:
            _salvar_health(health)
        return 0, 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        resultados = list(zip(fatia, pool.map(_validar_email, fatia)))

    with _lock:
        health = _carregar_health()
        for email, res in resultados:
            health[email] = res
        # Limpa entradas órfãs (emails que não existem mais na base)
        emails_ativos = set(emails)
        for k in list(health.keys()):
            if k not in emails_ativos:
                health.pop(k, None)
        _salvar_health(health)

    validos = sum(1 for _, r in resultados if r.get("status") == "valid")
    return len(fatia), validos


def _loop() -> None:
    logger.info(
        f"[email_validation] worker iniciado — tick={TICK_SEGUNDOS}s · "
        f"fatia={EMAILS_POR_TICK} · workers={WORKERS} · "
        f"re-check={INTERVALO_RE_CHECK//86400}d · "
        f"lib={'ok' if _LIB_DISPONIVEL else 'AUSENTE'}"
    )
    while True:
        if _circuit_breaker.deve_pausar():
            time.sleep(min(30, max(1, _circuit_breaker.segundos_restantes())))
            continue
        try:
            n, validos = _tick()
            _circuit_breaker.registrar_sucesso()
            if n:
                logger.info(f"[email_validation] tick: {n} emails validados, {validos} OK")
        except Exception as e:
            _circuit_breaker.registrar_falha(e)
        time.sleep(TICK_SEGUNDOS)


def estado_circuit_breaker() -> dict:
    return _circuit_breaker.estado()


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def iniciar_worker() -> None:
    """Inicia thread daemon (idempotente)."""
    global _thread_ref
    if _thread_ref and _thread_ref.is_alive():
        return
    if not _LIB_DISPONIVEL:
        logger.warning("[email_validation] lib email-validator ausente — worker não iniciará")
        return
    t = threading.Thread(target=_loop, name="email-validation-worker", daemon=True)
    t.start()
    _thread_ref = t


# ---------------------------------------------------------------------------
# Modo standalone (debug)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"[email_validation] standalone · lib={'ok' if _LIB_DISPONIVEL else 'AUSENTE'}")
    emails = _listar_emails()
    print(f"Total emails na base: {len(emails)}")
    health = _carregar_health()
    fatia = _selecionar_fatia(emails, health, 20)
    if not fatia:
        fatia = emails[:10]
    for e in fatia:
        r = _validar_email(e)
        icon = {"valid": "[OK]", "no_mx": "[MX?]", "invalid": "[X]", "erro": "[?]"}.get(r["status"], "[?]")
        print(f"  {icon} {e:50s} → {r['status']:8s} · {r.get('razao', '')[:80]}")
