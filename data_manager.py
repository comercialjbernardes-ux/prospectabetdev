"""
data_manager.py — Estado global de dados + carregamento + overrides + merges
=============================================================================
Centraliza tudo relacionado a:
  - estado in-memory `_dados` (lista de registros, thread-safe via RLock)
  - leitura do `bets_enriquecidas.json` (ou CSV fallback)
  - overrides (edições manuais persistidas em `dados/overrides.json`)
  - cache TTL+mtime dos arquivos `*_health.json` (URL, afiliados, RA)
  - função `recarregar_dados()` que orquestra tudo

API pública:
    dados_snapshot() -> list[dict]              — cópia thread-safe
    recarregar() -> None                        — recarrega tudo do disco
    invalidar_cache_health() -> None            — força próxima leitura de disco
    aplicar_url_health(registros) -> None       — mescla url_health.json
    aplicar_afiliados_health(registros) -> None — mescla afiliados_health.json
    aplicar_reclame_aqui_health(registros) -> None — mescla reclame_aqui_health.json
    carregar_overrides() -> dict[str, dict]
    salvar_overrides(overrides) -> None
    aplicar_overrides(registros, overrides) -> None
    info_recarga() -> dict                      — para o endpoint /health

Constantes públicas:
    ARQUIVO_JSON, ARQUIVO_CSV, ARQUIVO_OVERRIDES
    CAMPOS_EDITAVEIS — set de campos válidos para /api/editar
    STATUS_INATIVO   — status que marcam site como inacessível
    STATUS_AFILIADOS_SIM, STATUS_AFILIADOS_NAO
"""

from __future__ import annotations

import csv as _csv
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable

import json_store
import health_score
from logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constantes / paths
# ---------------------------------------------------------------------------

ARQUIVO_JSON:      Path = Path("dados/bets_enriquecidas.json")
ARQUIVO_CSV:       Path = Path("bets_com_emails.csv")
ARQUIVO_OVERRIDES: Path = Path("dados/overrides.json")

CAMPOS_EDITAVEIS: set[str] = {
    "email_contato", "url", "marca", "razao_social",
    "cnpj", "uf", "municipio", "url_afiliados", "observacao",
}

# Status que indicam site INACESSÍVEL (offline/DNS falhou/timeout)
# "bloqueado" = ATIVO mas bloqueando bots (403/4xx) → NÃO é inativo
STATUS_INATIVO: set[str] = {"erro_http", "erro_conexao", "erro_ssl", "erro_dns", "timeout", "erro"}

STATUS_AFILIADOS_SIM: set[str] = {"encontrado_completo", "encontrado_url", "encontrado_email"}
STATUS_AFILIADOS_NAO: set[str] = {"nao_encontrado", "bloqueado_robots"}

SCHEMA_OVERRIDES_VERSION: int = 1

# ---------------------------------------------------------------------------
# Estado global thread-safe
# ---------------------------------------------------------------------------

_dados:         list[dict] = []
_dados_lock                = RLock()

_overrides:     dict[str, dict] = {}
_overrides_lock                 = Lock()

_ultima_recarga_ts: float = 0.0

# Cache TTL+mtime para health JSONs (etapa 1.2)
_health_cache:      dict[str, tuple[float, float, dict]] = {}
_health_cache_lock                                       = Lock()
_HEALTH_CACHE_TTL:  float = 10.0


# ---------------------------------------------------------------------------
# Snapshot / state access
# ---------------------------------------------------------------------------

def dados_snapshot() -> list[dict]:
    """Retorna cópia thread-safe da lista de dados (evita race com workers)."""
    with _dados_lock:
        return list(_dados)


def info_recarga() -> dict:
    """Info usada por /health: timestamp + count."""
    return {
        "ultima_recarga_ts": _ultima_recarga_ts,
        "total_registros":   len(_dados),
    }


# ---------------------------------------------------------------------------
# Cache de health JSONs (TTL 10s + invalidação por mtime)
# ---------------------------------------------------------------------------

def invalidar_cache_health() -> None:
    """Limpa o cache TTL dos health JSONs (ex: após recarregar_dados)."""
    with _health_cache_lock:
        _health_cache.clear()


def _ler_health_cached(
    chave: str,
    path: Path,
    reader: Callable[[], dict] | None = None,
) -> dict:
    """
    Leitura com cache TTL 10s + invalidação por mtime.

    :param chave: identificador único (ex: 'url', 'afiliados', 'ra')
    :param path:  caminho do JSON (mtime check)
    :param reader: callable opcional; default lê path direto com json.loads
    :returns: dict de saúde; {} em qualquer falha
    """
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    agora = time.monotonic()
    with _health_cache_lock:
        cached = _health_cache.get(chave)
        if cached and cached[1] == mtime and (agora - cached[0]) < _HEALTH_CACHE_TTL:
            return cached[2]
    try:
        data = reader() if reader is not None else json.loads(path.read_text("utf-8"))
    except Exception:
        logger.exception(f"Falha lendo health '{chave}' de {path}")
        return {}
    if not isinstance(data, dict):
        data = {}
    with _health_cache_lock:
        _health_cache[chave] = (agora, mtime, data)
    return data


# ---------------------------------------------------------------------------
# Overrides (edições manuais persistidas em dados/overrides.json)
# ---------------------------------------------------------------------------

def carregar_overrides() -> dict[str, dict]:
    """Lê overrides.json. Em caso de JSON corrompido, faz backup e retorna {}."""
    if not ARQUIVO_OVERRIDES.exists():
        return {}
    try:
        data = json.loads(ARQUIVO_OVERRIDES.read_text("utf-8"))
    except json.JSONDecodeError:
        backup = ARQUIVO_OVERRIDES.with_suffix(f".json.corrupto_{int(time.time())}")
        try:
            shutil.copy2(ARQUIVO_OVERRIDES, backup)
        except Exception:
            pass
        logger.error(f"overrides.json corrompido — backup em {backup}")
        return {}
    except OSError:
        return {}
    versao = data.get("_schema_version", 1)
    if versao < SCHEMA_OVERRIDES_VERSION:
        data = _migrar_overrides(data, de=versao, para=SCHEMA_OVERRIDES_VERSION)
    return {k: v for k, v in data.items() if not k.startswith("_schema")}


def salvar_overrides(overrides: dict[str, dict]) -> None:
    """Grava overrides.json atomicamente (tmp → rename) com backup .bak."""
    ARQUIVO_OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARQUIVO_OVERRIDES.with_suffix(".json.tmp")
    payload = {
        "_schema_version": SCHEMA_OVERRIDES_VERSION,
        "_salvo_em":       datetime.now().isoformat(timespec="seconds"),
        **overrides,
    }
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        if ARQUIVO_OVERRIDES.exists():
            shutil.copy2(ARQUIVO_OVERRIDES, ARQUIVO_OVERRIDES.with_suffix(".json.bak"))
        tmp.replace(ARQUIVO_OVERRIDES)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _migrar_overrides(data: dict, de: int, para: int) -> dict:
    """Stub para futuras migrações de schema de overrides."""
    return data


def aplicar_overrides(registros: list[dict], overrides: dict[str, dict]) -> None:
    """Mescla overrides nos registros — marca cada registro editado."""
    for r in registros:
        cnpj = (r.get("cnpj") or "").strip()
        if not cnpj or cnpj not in overrides:
            continue
        ov = overrides[cnpj]
        campos_editados: list[str] = []
        for campo, valor in ov.items():
            if campo.startswith("_") or campo not in CAMPOS_EDITAVEIS:
                continue
            r[campo] = valor
            campos_editados.append(campo)
            if campo == "email_contato":
                if valor:
                    if r.get("status") in (None, "", "nao_encontrado", "erro_conexao",
                                           "bloqueado_robots", "sem_url"):
                        r["status"] = "encontrado_manual"
                else:
                    r["status"] = "nao_encontrado"
            if campo == "url_afiliados":
                if valor:
                    r["status_afiliados"]   = "encontrado_manual"
                    r["_afiliados_status"]  = "encontrado_manual"
                    r["_afiliados_display"] = "sim"
                    r["_afiliados_url"]     = valor
                else:
                    r["status_afiliados"] = ""
                    r.pop("_afiliados_status",  None)
                    r.pop("_afiliados_display", None)
                    r.pop("_afiliados_url",     None)
        if campos_editados:
            r["_editado_manualmente"] = True
            r["_campos_editados"]     = campos_editados
            r["_editado_em"]          = ov.get("_edited_at", "")


# ---------------------------------------------------------------------------
# Carregamento de dados base
# ---------------------------------------------------------------------------

def carregar_dados() -> list[dict]:
    """Lê bets_enriquecidas.json (preferido) ou CSV fallback."""
    dados = json_store.ler(ARQUIVO_JSON, default=None)
    if dados is not None:
        for r in dados:
            try:
                r["capital_social"] = float(r.get("capital_social") or 0)
            except (ValueError, TypeError):
                r["capital_social"] = 0.0
        return dados
    if ARQUIVO_CSV.exists():
        registros: list[dict] = []
        with open(ARQUIVO_CSV, encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                registros.append(row)
        return registros
    return []


# ---------------------------------------------------------------------------
# Merges de health (workers daemons)
# ---------------------------------------------------------------------------

def _display_afiliados(status: str) -> str:
    if status in STATUS_AFILIADOS_SIM: return "sim"
    if status in STATUS_AFILIADOS_NAO: return "nao"
    return "nao_encontrado"


def aplicar_afiliados_health(registros: list[dict]) -> None:
    """Mescla dados de afiliados_health.json (cacheado)."""
    path = Path("dados/afiliados_health.json")
    dados_h = _ler_health_cached("afiliados", path)
    if not dados_h:
        for r in registros:
            if r.get("_afiliados_status") != "encontrado_manual":
                if "_afiliados_display" not in r:
                    r["_afiliados_display"] = "nao_encontrado"
        return
    for r in registros:
        if r.get("_afiliados_status") == "encontrado_manual":
            continue
        u    = (r.get("url") or "").strip()
        info = dados_h.get(u)
        if not info:
            r["_afiliados_display"] = "nao_encontrado"
            continue
        st = info.get("status", "")
        r["_afiliados_status"]  = st
        r["_afiliados_display"] = _display_afiliados(st)
        r["_afiliados_url"]     = info.get("url_afiliado", "")
        r["_afiliados_ts"]      = info.get("checado_em", "")


def aplicar_reclame_aqui_health(registros: list[dict], ra_module: Any) -> None:
    """Mescla dados do Reclame Aqui (reclame_aqui_health.json) cacheado."""
    if ra_module is None:
        for r in registros:
            r.setdefault("_ra_status", "desconhecido")
        return
    dados_h = _ler_health_cached(
        "ra",
        Path("dados/reclame_aqui_health.json"),
        reader=ra_module.ler_health,
    )
    for r in registros:
        slug = ra_module.slug_para_marca(r.get("marca") or "")
        info = dados_h.get(slug)
        if not info:
            r["_ra_status"] = "desconhecido"
            continue
        r["_ra_status"]      = info.get("status", "desconhecido")
        r["_ra_nota"]        = info.get("nota")
        r["_ra_reclamacoes"] = info.get("total_reclamacoes")
        r["_ra_resolvidas"]  = info.get("percentual_resolvidas")
        r["_ra_reputacao"]   = info.get("reputacao", "")
        r["_ra_ra1000"]      = info.get("ra1000", False)
        r["_ra_url"]         = info.get("url_reclame_aqui", "")
        r["_ra_ts"]          = info.get("checado_em", "")


def aplicar_email_validation(registros: list[dict], ev_module: Any) -> None:
    """
    Mescla status de validação de email em cada registro (etapa 7).
    Escreve `_email_validation_status` (valid|no_mx|invalid|erro|sem_email).
    """
    if ev_module is None:
        for r in registros:
            r["_email_validation_status"] = "sem_email" if not r.get("email_contato") else "desconhecido"
        return
    try:
        health = ev_module.ler_health()
    except Exception:
        health = {}
    for r in registros:
        email = (r.get("email_contato") or "").strip().lower()
        if not email:
            r["_email_validation_status"] = "sem_email"
            continue
        info = health.get(email)
        if not info:
            r["_email_validation_status"] = "desconhecido"
            continue
        r["_email_validation_status"] = info.get("status", "desconhecido")
        r["_email_validation_ts"]     = info.get("checado_em", "")
        r["_email_validation_razao"]  = info.get("razao", "")


def aplicar_url_health(registros: list[dict], url_health_module: Any) -> None:
    """Mescla url_health.json (cacheado). Normaliza erro_http 4xx → 'bloqueado'."""
    health = _ler_health_cached(
        "url",
        Path("dados/url_health.json"),
        reader=url_health_module.ler_health,
    )
    for r in registros:
        u    = (r.get("url") or "").strip()
        info = health.get(u)
        if not info:
            r["_url_health_status"] = "desconhecido"
            r["_url_inativa"]       = False
            continue
        st_raw    = info.get("status", "desconhecido")
        http_code = info.get("http_code", 0)
        # erro_http com código 4xx = site ativo bloqueando bots (não inativo)
        if st_raw == "erro_http" and 0 < http_code < 500:
            st = "bloqueado"
        else:
            st = st_raw
        r["_url_health_status"] = st
        r["_url_http_code"]     = http_code
        r["_url_checked_at"]    = info.get("checado_em", "")
        r["_url_latencia_ms"]   = info.get("latencia_ms", 0)
        if info.get("redirecionou"):
            r["_url_redirect_to"] = info.get("url_final", "")
        r["_url_inativa"] = (st in STATUS_INATIVO) or bool(r.get("_removido_do_csv"))


# ---------------------------------------------------------------------------
# Orquestração: recarregar_dados
# ---------------------------------------------------------------------------

def recarregar(
    url_health_module: Any,
    ra_module: Any,
    stats_snapshot_module: Any | None = None,
    email_validation_module: Any | None = None,
) -> None:
    """
    Recarrega dados + overrides + aplica todos os merges.
    Os módulos de workers são injetados (evita acoplamento circular).
    """
    global _dados, _overrides, _ultima_recarga_ts
    invalidar_cache_health()
    _overrides = carregar_overrides()
    dados = carregar_dados()
    aplicar_overrides(dados, _overrides)
    aplicar_url_health(dados, url_health_module)
    aplicar_afiliados_health(dados)
    aplicar_reclame_aqui_health(dados, ra_module)
    aplicar_email_validation(dados, email_validation_module)
    # Score composto — calculado DEPOIS dos merges, escreve _health_score em cada registro
    health_score.aplicar_em_lote(dados)
    with _dados_lock:
        _dados = dados
    _ultima_recarga_ts = time.time()
    if stats_snapshot_module is not None:
        try:
            stats_snapshot_module.registrar_snapshot_se_necessario(dados)
        except Exception:
            logger.exception("Falha ao registrar snapshot")


def overrides_atuais() -> dict[str, dict]:
    """Retorna cópia thread-safe dos overrides atuais."""
    with _overrides_lock:
        return dict(_overrides)


def atualizar_override(cnpj: str, novo: dict) -> dict[str, dict]:
    """
    Atualiza/insere um override para um CNPJ. Persiste em disco.
    Retorna a versão final do dict de overrides.
    """
    global _overrides
    with _overrides_lock:
        existente = _overrides.get(cnpj, {})
        existente.update(novo)
        _overrides[cnpj] = existente
        salvar_overrides(_overrides)
        return dict(_overrides)
