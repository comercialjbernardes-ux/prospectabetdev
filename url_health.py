"""
url_health.py — Worker de validação contínua de URLs
=====================================================

Thread daemon que re-valida periodicamente as URLs dos registros em
`dados/bets_enriquecidas.json` + overrides, salvando o estado de saúde
em `dados/url_health.json`.

Estratégia:
- A cada TICK_SEGUNDOS, pega as URLS_POR_TICK URLs mais antigas (ou ainda
  não checadas) e valida em paralelo (WORKERS threads).
- URL válida: HTTP 2xx/3xx sem redirect externo ou redirect para subdomínio
  do mesmo host base.
- Redirect externo: segue para host diferente da raiz (ex.: moveu de domínio).
- Timeout, DNS, SSL, 4xx, 5xx → categorizados em `status`.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
import json_store
from logging_config import get_logger
from worker_utils import CircuitBreaker

logger = get_logger(__name__)
_circuit_breaker = CircuitBreaker("url_health", logger=logger)

# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------

TICK_SEGUNDOS = 60        # era 10 — reduzido para aliviar CPU na madrugada
URLS_POR_TICK = 10
WORKERS = 2               # era 5 — reduzido para limitar threads simultâneas
TIMEOUT_CONNECT = 6
TIMEOUT_READ = 8
INTERVALO_RE_CHECK = 180  # segundos

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ARQUIVO_DADOS = Path("dados/bets_enriquecidas.json")
ARQUIVO_OVERRIDES = Path("dados/overrides.json")
ARQUIVO_HEALTH = Path("dados/url_health.json")

# Auto-aplica redirect permanente (301/308) como override de URL
AUTO_APLICAR_REDIRECT_PERMANENTE = True

_lock = threading.Lock()
_lock_overrides = threading.Lock()
_thread_ref: threading.Thread | None = None


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _carregar_health() -> dict:
    return json_store.ler(ARQUIVO_HEALTH, default={}) or {}


def _salvar_health(health: dict) -> None:
    json_store.salvar(ARQUIVO_HEALTH, health)


def _listar_urls() -> list[str]:
    """URLs atuais — do JSON base + overrides (overrides vencem)."""
    urls: set[str] = set()
    for r in (json_store.ler(ARQUIVO_DADOS, default=[]) or []):
        u = (r.get("url") or "").strip()
        if u and u.startswith(("http://", "https://")):
            urls.add(u)

    ov_raw = json_store.ler(ARQUIVO_OVERRIDES, default={}) or {}
    for chave, ov in ov_raw.items():
        # Ignora metadados do schema (ex: '_schema_version', '_salvo_em')
        if chave.startswith("_") or not isinstance(ov, dict):
            continue
        u = (ov.get("url") or "").strip()
        if u and u.startswith(("http://", "https://")):
            urls.add(u)
    return sorted(urls)


# ---------------------------------------------------------------------------
# Validação de uma URL
# ---------------------------------------------------------------------------


def _raiz_dominio(host: str) -> str:
    """Extrai 'bet.br' de 'www.foo.bet.br' (2 últimos tokens)."""
    if not host:
        return ""
    partes = host.lower().split(".")
    return ".".join(partes[-2:]) if len(partes) >= 2 else host.lower()


def _validar_url(url: str) -> dict:
    """
    Retorna dict com status/http_code/url_final/redirecionou/latencia_ms.
    Categorias de status:
      ok, redirect, erro_http, erro_conexao, erro_ssl, erro_dns, timeout, erro
    """
    t0 = time.time()
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    host_orig = _raiz_dominio(urlparse(url).hostname or "")

    def _latencia() -> int:
        return int((time.time() - t0) * 1000)

    def _resultado_ok(resp) -> dict:
        url_final = resp.url or url
        host_final = _raiz_dominio(urlparse(url_final).hostname or "")
        redirecionou = bool(resp.history) and host_final != host_orig
        # Detecta se a PRIMEIRA etapa do redirect foi permanente (301/308)
        redirect_permanente = False
        if resp.history:
            try:
                redirect_permanente = resp.history[0].status_code in (301, 308)
            except (AttributeError, IndexError):
                pass
        return {
            "status": "redirect" if redirecionou else "ok",
            "http_code": resp.status_code,
            "url_final": url_final,
            "redirecionou": redirecionou,
            "redirect_permanente": redirect_permanente,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }

    try:
        # HEAD primeiro (mais leve)
        resp = requests.head(
            url,
            headers=headers,
            timeout=(TIMEOUT_CONNECT, TIMEOUT_READ),
            allow_redirects=True,
        )
        # Alguns servidores rejeitam HEAD com 4xx — fallback para GET
        if resp.status_code in (400, 401, 403, 405, 406, 501):
            raise requests.HTTPError("HEAD rejeitado, tentar GET")
        if resp.status_code < 400:
            return _resultado_ok(resp)
        # 5xx via HEAD: servidor com erro
        return {
            "status": "erro_http",
            "http_code": resp.status_code,
            "url_final": resp.url or url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }
    except requests.HTTPError:
        pass
    except requests.exceptions.SSLError:
        return {
            "status": "erro_ssl",
            "http_code": 0,
            "url_final": url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }
    except requests.exceptions.ConnectTimeout:
        return {
            "status": "timeout",
            "http_code": 0,
            "url_final": url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }
    except requests.exceptions.ConnectionError as e:
        # Distingue DNS de outros erros de conexão
        msg = str(e).lower()
        status = "erro_dns" if "name or service" in msg or "getaddrinfo" in msg \
            else "erro_conexao"
        return {
            "status": status,
            "http_code": 0,
            "url_final": url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }
    except requests.RequestException:
        pass

    # Fallback: GET streaming (fecha logo após headers)
    try:
        with requests.get(
            url,
            headers=headers,
            timeout=(TIMEOUT_CONNECT, TIMEOUT_READ),
            allow_redirects=True,
            stream=True,
        ) as resp:
            if resp.status_code < 400:
                return _resultado_ok(resp)
            # 4xx = servidor respondeu (site ATIVO), mas recusou acesso (bot bloqueado)
            # 5xx = erro no servidor (pode estar inativo)
            status_http = "bloqueado" if resp.status_code < 500 else "erro_http"
            return {
                "status": status_http,
                "http_code": resp.status_code,
                "url_final": resp.url or url,
                "redirecionou": False,
                "checado_em": datetime.now().isoformat(timespec="seconds"),
                "latencia_ms": _latencia(),
            }
    except requests.exceptions.SSLError:
        return {
            "status": "erro_ssl",
            "http_code": 0,
            "url_final": url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }
    except requests.exceptions.Timeout:
        return {
            "status": "timeout",
            "http_code": 0,
            "url_final": url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }
    except requests.exceptions.ConnectionError as e:
        msg = str(e).lower()
        status = "erro_dns" if "name or service" in msg or "getaddrinfo" in msg \
            else "erro_conexao"
        return {
            "status": status,
            "http_code": 0,
            "url_final": url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }
    except Exception:
        return {
            "status": "erro",
            "http_code": 0,
            "url_final": url,
            "redirecionou": False,
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "latencia_ms": _latencia(),
        }


# ---------------------------------------------------------------------------
# Seleção de fatia e loop
# ---------------------------------------------------------------------------


def _selecionar_fatia(urls: list[str], health: dict, n: int) -> list[str]:
    """Retorna até n URLs priorizando nunca-checadas e mais antigas."""
    agora = datetime.now()

    def _idade(u: str) -> float:
        info = health.get(u)
        if not info or "checado_em" not in info:
            return float("inf")  # nunca checada → prioridade máxima
        try:
            t = datetime.fromisoformat(info["checado_em"])
            return (agora - t).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    # Filtra só as que podem ser re-checadas (idade > INTERVALO_RE_CHECK)
    # OU nunca checadas (idade == inf)
    candidatas = [(u, _idade(u)) for u in urls]
    candidatas = [
        (u, idade) for u, idade in candidatas
        if idade == float("inf") or idade >= INTERVALO_RE_CHECK
    ]
    candidatas.sort(key=lambda x: -x[1])  # mais antigas primeiro
    return [u for u, _ in candidatas[:n]]


def _aplicar_redirect_como_override(url_original: str, url_final: str) -> bool:
    """
    Grava `url_final` como override manual do registro cuja URL bate com
    `url_original`. Retorna True se aplicou override novo.
    """
    if not url_original or not url_final or url_original == url_final:
        return False

    # Acha o CNPJ do registro que tem essa URL
    if not ARQUIVO_DADOS.exists():
        return False
    try:
        with open(ARQUIVO_DADOS, encoding="utf-8") as f:
            base = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    cnpj_alvo = None
    for r in base:
        if (r.get("url") or "").strip() == url_original:
            cnpj_alvo = (r.get("cnpj") or "").strip()
            break
    if not cnpj_alvo:
        return False

    with _lock_overrides:
        overrides = {}
        if ARQUIVO_OVERRIDES.exists():
            try:
                with open(ARQUIVO_OVERRIDES, encoding="utf-8") as f:
                    overrides = json.load(f) or {}
            except (json.JSONDecodeError, OSError):
                pass

        reg_ov = overrides.get(cnpj_alvo, {})
        # Não sobrescreve se o usuário já editou manualmente a URL
        if reg_ov.get("url") and reg_ov.get("url") != url_original:
            return False
        # Não re-aplica se já está lá
        if reg_ov.get("url") == url_final:
            return False

        reg_ov["url"] = url_final
        reg_ov["_edited_at"] = datetime.now().isoformat(timespec="seconds")
        reg_ov["_auto_redirect"] = True
        overrides[cnpj_alvo] = reg_ov

        ARQUIVO_OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
        tmp = ARQUIVO_OVERRIDES.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(overrides, f, ensure_ascii=False, indent=2)
        tmp.replace(ARQUIVO_OVERRIDES)

    print(f"[url_health] auto-redirect: {url_original} → {url_final}")
    return True


_STATUS_FALHA = {"erro_http", "erro_conexao", "erro_ssl", "erro_dns", "timeout", "erro"}
_ALERTA_RECORRENCIA = 3        # nº de falhas em 24h para disparar alerta
_ALERTA_JANELA_SEG  = 86400    # 24h
_ALERTA_COOLDOWN    = 86400    # não re-dispara para a mesma URL em <24h


def _detectar_alerta_url_down(url: str, novo: dict, antigo: dict) -> dict:
    """
    Atualiza histórico de falhas e dispara alerta se ≥3 falhas em 24h
    (e nenhum alerta já disparado nas últimas 24h).
    Retorna o `novo` com `_historico_falhas` e `_alerta_disparado_em` mesclados.
    """
    historico = list(antigo.get("_historico_falhas") or [])
    agora = time.time()
    # Adiciona timestamp se foi falha agora
    if novo.get("status") in _STATUS_FALHA:
        historico.append(novo.get("checado_em") or datetime.now().isoformat(timespec="seconds"))

    # Mantém apenas os últimos N timestamps dentro da janela 24h
    def _ts_to_epoch(ts: str) -> float:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "")).timestamp()
        except (ValueError, TypeError):
            return 0.0
    historico = [ts for ts in historico if (agora - _ts_to_epoch(ts)) < _ALERTA_JANELA_SEG]
    historico = historico[-10:]   # cap absoluto de 10 entradas
    novo["_historico_falhas"] = historico

    # Decide se dispara alerta
    ultimo_alerta = antigo.get("_alerta_disparado_em") or ""
    ultimo_epoch  = _ts_to_epoch(ultimo_alerta) if ultimo_alerta else 0.0
    em_cooldown = (agora - ultimo_epoch) < _ALERTA_COOLDOWN

    if len(historico) >= _ALERTA_RECORRENCIA and not em_cooldown:
        novo["_alerta_disparado_em"] = datetime.now().isoformat(timespec="seconds")
        try:
            from notificacoes import notificar_evento
            notificar_evento(
                tipo="url_down",
                titulo=f"⚠️ URL caindo recorrente — {url}",
                campos={
                    "url":           url,
                    "falhas_24h":    len(historico),
                    "ultima_status": novo.get("status", "?"),
                    "http_code":     novo.get("http_code", 0),
                    "primeira_falha": historico[0] if historico else "?",
                },
            )
            logger.warning(f"[alerta] URL down recorrente: {url} ({len(historico)} falhas em 24h)")
        except Exception:
            logger.exception("Falha ao disparar alerta url_down")
    else:
        # Preserva campo de alerta anterior se existir (não limpa só por sucesso único)
        if ultimo_alerta:
            novo["_alerta_disparado_em"] = ultimo_alerta
    return novo


def _tick() -> int:
    """Executa um tick. Retorna quantas URLs foram validadas."""
    urls = _listar_urls()
    with _lock:
        health = _carregar_health()

    fatia = _selecionar_fatia(urls, health, URLS_POR_TICK)
    if not fatia:
        return 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        resultados = list(zip(fatia, pool.map(_validar_url, fatia)))

    with _lock:
        health = _carregar_health()  # recarrega (thread-safe)
        for url, res in resultados:
            antigo = health.get(url) or {}
            res = _detectar_alerta_url_down(url, res, antigo)
            health[url] = res
        # Limpa entradas órfãs (URL que não existe mais na base)
        urls_ativas = set(urls)
        for u in list(health.keys()):
            if u not in urls_ativas:
                health.pop(u, None)
        _salvar_health(health)

    # B — auto-aplica redirects permanentes (301/308) como override
    if AUTO_APLICAR_REDIRECT_PERMANENTE:
        for url_orig, res in resultados:
            if res.get("status") == "redirect" and res.get("redirect_permanente"):
                url_final = (res.get("url_final") or "").rstrip("/")
                if url_final and url_final != url_orig.rstrip("/"):
                    _aplicar_redirect_como_override(url_orig, url_final)

    return len(fatia)


def _loop() -> None:
    logger.info(f"[url_health] worker iniciado — tick={TICK_SEGUNDOS}s · "
                f"fatia={URLS_POR_TICK} · workers={WORKERS}")
    while True:
        if _circuit_breaker.deve_pausar():
            # Circuito aberto: dorme até reabrir, mas em pedaços de até 30s
            time.sleep(min(30, max(1, _circuit_breaker.segundos_restantes())))
            continue
        try:
            n = _tick()
            _circuit_breaker.registrar_sucesso()
            if n:
                logger.info(f"[url_health] tick: {n} URLs validadas")
        except Exception as e:
            _circuit_breaker.registrar_falha(e)
        time.sleep(TICK_SEGUNDOS)


def estado_circuit_breaker() -> dict:
    """Expõe estado atual do circuit breaker (para /health endpoint)."""
    return _circuit_breaker.estado()


# ---------------------------------------------------------------------------
# Boot público
# ---------------------------------------------------------------------------


def iniciar_worker() -> None:
    """Inicia a thread daemon (idempotente)."""
    global _thread_ref
    if _thread_ref and _thread_ref.is_alive():
        return
    t = threading.Thread(target=_loop, name="url-health-worker", daemon=True)
    t.start()
    _thread_ref = t


def ler_health() -> dict:
    """Leitura thread-safe do arquivo de saúde."""
    with _lock:
        return _carregar_health()


if __name__ == "__main__":
    # Modo standalone: roda o worker sem o Flask (útil p/ debug)
    iniciar_worker()
    while True:
        time.sleep(60)
