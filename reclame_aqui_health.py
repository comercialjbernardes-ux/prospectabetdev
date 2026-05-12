"""
reclame_aqui_health.py — Worker de análise de confiabilidade via Reclame Aqui
=============================================================================

Thread daemon que, a cada TICK_SEGUNDOS, consulta o Reclame Aqui para
MARCAS_POR_TICK bets, extraindo nota, reputação e contagem de reclamações.

Estratégia de coleta (curl_cffi + scraping HTML):
  Scraping da página HTML da empresa com curl_cffi (impersonate Chrome) +
  extração do bloco __NEXT_DATA__ (Next.js SSR).
  Campos extraídos de: company.performanceData (dict) e
  company.companyIndex6Months/12Months (Java toString string).

Chave do JSON de saída: slug primário da marca (não URL).
  Ex: "VAIDEBET" → chave "vaidebet"
      "JOGA JUNTO" → chave "joga-junto"
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from logging_config import get_logger
from worker_utils import CircuitBreaker

logger = get_logger(__name__)
_circuit_breaker = CircuitBreaker("reclame_aqui", logger=logger)

# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------

TICK_SEGUNDOS     = 120     # Intervalo entre ticks (rate-limit amigável)
MARCAS_POR_TICK   = 3       # Bets consultadas por tick
WORKERS           = 2       # Threads paralelas
INTERVALO_RE_CHECK = 86400  # Re-checa a cada 24h (dados mudam lentamente)

ARQUIVO_DADOS   = Path("dados/bets_enriquecidas.json")
ARQUIVO_HEALTH  = Path("dados/reclame_aqui_health.json")

# User-Agent para curl_cffi (impersonate sobrepõe, mas mantemos p/ fallback)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_REPUTACAO_LABELS = {
    "RA1000":   "RA1000",
    "GREAT":    "Ótimo",
    "GOOD":     "Bom",
    "REGULAR":  "Regular",
    "BAD":      "Ruim",
    "TERRIBLE": "Péssimo",
    "NO_INDEX":        "Sem índice",     # empresa cadastrada mas sem volume suficiente
    "NOINDEX":         "Sem índice",
    "NOT_RECOMMENDED": "Não recomendada",  # empresa não responde reclamações
    "NOTRECOMMENDED":  "Não recomendada",
    # variantes em português retornadas pela API
    "OTIMO":    "Ótimo",
    "BOM":      "Bom",
    "PESSIMO":  "Péssimo",
}

_lock = threading.Lock()
_thread_ref: threading.Thread | None = None

# Guard de importação curl_cffi
try:
    from curl_cffi import requests as _cffi_req
    _CURL_CFFI_OK = True
except ImportError:
    _CURL_CFFI_OK = False


# ---------------------------------------------------------------------------
# Utilitários de slug
# ---------------------------------------------------------------------------


def _marca_para_slugs(marca: str) -> list[str]:
    """
    Gera lista de variantes de slug a partir do nome da marca.
    Retorna slugs em ordem de tentativa (mais específico → mais simples).

    Exemplos:
      "VAIDEBET"    → ["vaidebet"]
      "JOGA JUNTO"  → ["joga-junto", "jogajunto", "joga"]
      "BET365"      → ["bet365", "bet"]
      "F12.BET"     → ["f12-bet", "f12bet", "f12"]
    """
    if not marca:
        return []
    # Normaliza: remove acentos, minúsculo, substitui não-alfanum por hífens
    norm = unicodedata.normalize("NFKD", marca.lower())
    norm = norm.encode("ascii", "ignore").decode()
    norm = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    if not norm:
        return []
    variantes: list[str] = [norm]
    # Sem hífens
    sem_hifen = norm.replace("-", "")
    if sem_hifen and sem_hifen != norm:
        variantes.append(sem_hifen)
    # Só o primeiro token (ex: "joga-junto" → "joga")
    primeiro = norm.split("-")[0]
    if primeiro and primeiro != norm and len(primeiro) >= 3:
        variantes.append(primeiro)
    return variantes


def _slug_principal(marca: str) -> str:
    slugs = _marca_para_slugs(marca)
    return slugs[0] if slugs else marca.lower().strip()


# ---------------------------------------------------------------------------
# I/O thread-safe
# ---------------------------------------------------------------------------


def _carregar_health() -> dict:
    if not ARQUIVO_HEALTH.exists():
        return {}
    try:
        with open(ARQUIVO_HEALTH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _salvar_health(data: dict) -> None:
    ARQUIVO_HEALTH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARQUIVO_HEALTH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(ARQUIVO_HEALTH)


def _listar_marcas() -> list[str]:
    """Retorna lista de marcas únicas dos registros."""
    if not ARQUIVO_DADOS.exists():
        return []
    try:
        with open(ARQUIVO_DADOS, encoding="utf-8") as f:
            base = json.load(f)
        marcas = sorted({
            (r.get("marca") or "").strip()
            for r in base
            if (r.get("marca") or "").strip()
        })
        return marcas
    except (json.JSONDecodeError, OSError):
        return []


# ---------------------------------------------------------------------------
# Extração de dados
# ---------------------------------------------------------------------------


def _normalizar_reputacao(raw: str | None) -> tuple[str, bool]:
    """Retorna (label_pt, is_ra1000) a partir de string bruta da API."""
    if not raw:
        return "", False
    upper = raw.upper().replace(" ", "").replace("Ó", "O").replace("É", "E")
    if upper == "RA1000":
        return "RA1000", True
    # Tentativa direta no dicionário (versão upper normalizada)
    label = _REPUTACAO_LABELS.get(upper)
    if label:
        return label, False
    # Tentativa com underscores removidos (ex: "NO_INDEX" → "NOINDEX")
    upper_nound = upper.replace("_", "")
    label = _REPUTACAO_LABELS.get(upper_nound)
    if label:
        return label, False
    # Fallback: capitaliza o valor bruto
    return raw.capitalize(), False


def _extrair_numero(valor) -> float | None:
    """Converte valor (str, int, float) para float, ou None se inválido."""
    if valor is None:
        return None
    try:
        return round(float(str(valor).replace(",", ".")), 1)
    except (ValueError, TypeError):
        return None


def _parse_java_kv(s: str, key: str):
    """
    Extrai um valor de string no formato Java Map.toString():
      "{key1=val1, key2=val2, ...}"
    Retorna o valor como float, int ou str (ou None se não encontrado).
    """
    if not s or not isinstance(s, str):
        return None
    m = re.search(rf'\b{re.escape(key)}=([^,}}]+)', s)
    if not m:
        return None
    val = m.group(1).strip()
    if val in ("null", "None", ""):
        return None
    # Tenta conversão numérica
    try:
        if "." in val:
            return float(val)
        return int(val)
    except (ValueError, TypeError):
        return val


def _extrair_dados_html(html: str, slug: str, url_page: str) -> dict | None:
    """
    Extrai dados do bloco __NEXT_DATA__ (Next.js SSR) da página HTML.

    Estrutura confirmada em reclameaqui.com.br/empresa/{slug}/:
      pageProps.company.performanceData  → dict com status, consumerScore,
          solvedPercentual, dealAgainPercentual, answeredPercentual, totalComplains
      pageProps.company.companyIndex6Months  → Java toString string com finalScore
      pageProps.company.companyIndex12Months → Java toString string com finalScore
      pageProps.company.complainCount        → int (total histórico)
    """
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None

    page_props = data.get("props", {}).get("pageProps", {})
    company = (
        page_props.get("company")
        or page_props.get("companyData")
        or {}
    )
    if not company or not isinstance(company, dict):
        return None

    # ── performanceData (dict confiável com dados de 6 meses) ────────────────
    perf = company.get("performanceData") or {}

    # ── companyIndex strings (Java toString) — para finalScore ───────────────
    idx6  = company.get("companyIndex6Months")  or ""
    idx12 = company.get("companyIndex12Months") or ""
    # Prefere 6M; usa 12M como fallback
    final_score_raw = (
        _parse_java_kv(idx6,  "finalScore")
        or _parse_java_kv(idx12, "finalScore")
    )

    # ── Campos extraídos ──────────────────────────────────────────────────────
    nota         = _extrair_numero(final_score_raw)
    # Fallback: consumerScore (nota dos consumidores)
    if nota is None:
        nota = _extrair_numero(perf.get("consumerScore"))
    # finalScore=0.0 com NO_INDEX = empresa sem avaliação suficiente
    if nota == 0.0:
        nota = None

    # Total reclamações: 12M (mais abrangente) → 6M → complainCount
    total_12m = _parse_java_kv(idx12, "totalComplains")
    total_6m  = _extrair_numero(perf.get("totalComplains"))
    total_rec_raw = total_12m or total_6m or company.get("complainCount")
    total_rec = int(total_rec_raw) if total_rec_raw is not None else None

    resolvidas  = _extrair_numero(perf.get("solvedPercentual"))
    respondidas = _extrair_numero(perf.get("answeredPercentual"))
    voltaria    = _extrair_numero(perf.get("dealAgainPercentual"))

    # Status (reputação): de performanceData ou do índice 6M
    rep_raw = (
        perf.get("status")
        or _parse_java_kv(idx6,  "status")
        or _parse_java_kv(idx12, "status")
        or ""
    )
    reputacao, ra1000 = _normalizar_reputacao(str(rep_raw) if rep_raw else "")

    if nota is None and total_rec is None:
        return None  # não conseguiu dados úteis

    return {
        "status":                 "encontrado",
        "slug":                   slug,
        "nota":                   nota,
        "total_reclamacoes":      total_rec,
        "percentual_respondidas": respondidas,
        "percentual_resolvidas":  resolvidas,
        "voltaria_comprar":       voltaria,
        "reputacao":              reputacao,
        "ra1000":                 ra1000,
        "url_reclame_aqui":       url_page,
        "checado_em":             datetime.now().isoformat(timespec="seconds"),
        "fonte":                  "html_nextdata",
    }


# ---------------------------------------------------------------------------
# Busca principal
# ---------------------------------------------------------------------------


def _buscar_reclame_aqui(marca: str) -> dict:
    """
    Consulta o Reclame Aqui para uma marca. Retorna dict com dados extraídos.
    Tenta slugs em ordem: primário, sem hífens, primeiro token.
    """
    now_iso = datetime.now().isoformat(timespec="seconds")

    if not _CURL_CFFI_OK:
        return {
            "status": "erro_configuracao",
            "checado_em": now_iso,
            "marca": marca,
            "erro": "curl_cffi não instalado",
        }

    slugs = _marca_para_slugs(marca)
    if not slugs:
        return {"status": "sem_marca", "checado_em": now_iso, "marca": marca}

    headers = {
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer": "https://www.reclameaqui.com.br/",
    }

    # ── Scraping HTML (página da empresa com __NEXT_DATA__) ───────────────
    for slug in slugs:
        try:
            url_page = f"https://www.reclameaqui.com.br/empresa/{slug}/"
            resp = _cffi_req.get(
                url_page,
                headers=headers,
                impersonate="chrome124",
                timeout=20,
            )
            if resp.status_code == 200:
                html = resp.text
                resultado = _extrair_dados_html(html, slug, url_page)
                if resultado:
                    resultado["marca"] = marca
                    return resultado
                # Página carregou mas não tem dados → empresa não encontrada
                if "não encontramos" in html.lower() or "not found" in html.lower():
                    break
        except Exception:
            pass

    return {
        "status":     "nao_encontrado",
        "checado_em": now_iso,
        "marca":      marca,
    }


# ---------------------------------------------------------------------------
# Seleção de fatia (prioriza nunca-checadas e mais antigas)
# ---------------------------------------------------------------------------


def _selecionar_fatia(marcas: list[str], health: dict, n: int) -> list[str]:
    agora = datetime.now()

    def _idade(m: str) -> float:
        slug = _slug_principal(m)
        info = health.get(slug)
        if not info or "checado_em" not in info:
            return float("inf")
        try:
            t = datetime.fromisoformat(info["checado_em"])
            return (agora - t).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    candidatas = [
        (m, _idade(m)) for m in marcas
        if _idade(m) == float("inf") or _idade(m) >= INTERVALO_RE_CHECK
    ]
    candidatas.sort(key=lambda x: -x[1])
    return [m for m, _ in candidatas[:n]]


# ---------------------------------------------------------------------------
# Tick e loop
# ---------------------------------------------------------------------------


_LIMIAR_QUEDA_NOTA = 0.5    # queda mínima da nota RA para disparar alerta


def _detectar_alerta_ra_queda(marca: str, novo: dict, antigo: dict) -> dict:
    """
    Se a nota RA caiu >= 0.5 desde a última verificação, dispara alerta.
    Mantém `_ultima_nota` no registro para comparação na próxima verificação.
    """
    nota_atual    = novo.get("nota")
    nota_anterior = antigo.get("nota") if antigo.get("status") == "encontrado" else None

    # Só compara se ambos os checks tiveram nota válida
    if nota_atual is not None and nota_anterior is not None:
        try:
            queda = float(nota_anterior) - float(nota_atual)
        except (TypeError, ValueError):
            queda = 0.0
        if queda >= _LIMIAR_QUEDA_NOTA:
            try:
                from notificacoes import notificar_evento
                notificar_evento(
                    tipo="ra_score_drop",
                    titulo=f"📉 Queda na nota Reclame Aqui — {marca}",
                    campos={
                        "marca":         marca,
                        "nota_anterior": nota_anterior,
                        "nota_atual":    nota_atual,
                        "queda":         round(queda, 2),
                        "url_ra":        novo.get("url_reclame_aqui", ""),
                        "reputacao":     novo.get("reputacao", ""),
                    },
                )
                logger.warning(f"[alerta] {marca}: nota RA caiu {queda:.2f} ({nota_anterior} → {nota_atual})")
            except Exception:
                logger.exception("Falha ao disparar alerta ra_score_drop")
    return novo


def _tick() -> tuple[int, int]:
    marcas = _listar_marcas()
    with _lock:
        health = _carregar_health()

    fatia = _selecionar_fatia(marcas, health, MARCAS_POR_TICK)
    if not fatia:
        return 0, 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        resultados = list(zip(fatia, pool.map(_buscar_reclame_aqui, fatia)))

    with _lock:
        health = _carregar_health()
        for marca, res in resultados:
            slug = _slug_principal(marca)
            antigo = health.get(slug) or {}
            res = _detectar_alerta_ra_queda(marca, res, antigo)
            health[slug] = res
        # Limpa entradas órfãs (marcas que não existem mais)
        slugs_ativos = {_slug_principal(m) for m in marcas}
        for k in list(health.keys()):
            if k not in slugs_ativos:
                health.pop(k, None)
        _salvar_health(health)

    encontrados = sum(1 for _, r in resultados if r.get("status") == "encontrado")
    return len(fatia), encontrados


def _loop() -> None:
    logger.info(
        f"[reclame_aqui] worker iniciado — tick={TICK_SEGUNDOS}s · "
        f"fatia={MARCAS_POR_TICK} · workers={WORKERS} · "
        f"re-check={INTERVALO_RE_CHECK//3600}h · "
        f"curl_cffi={'ok' if _CURL_CFFI_OK else 'AUSENTE'}"
    )
    while True:
        if _circuit_breaker.deve_pausar():
            time.sleep(min(30, max(1, _circuit_breaker.segundos_restantes())))
            continue
        try:
            n, enc = _tick()
            _circuit_breaker.registrar_sucesso()
            if n:
                logger.info(f"[reclame_aqui] tick: {n} marcas checadas, {enc} encontradas no RA")
        except Exception as e:
            _circuit_breaker.registrar_falha(e)
        time.sleep(TICK_SEGUNDOS)


def estado_circuit_breaker() -> dict:
    return _circuit_breaker.estado()


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def iniciar_worker() -> None:
    """Inicia a thread daemon (idempotente)."""
    global _thread_ref
    if _thread_ref and _thread_ref.is_alive():
        return
    t = threading.Thread(target=_loop, name="reclame-aqui-worker", daemon=True)
    t.start()
    _thread_ref = t


def ler_health() -> dict:
    """Leitura thread-safe do arquivo de saúde."""
    with _lock:
        return _carregar_health()


def slug_para_marca(marca: str) -> str:
    """Expõe _slug_principal para uso em app.py."""
    return _slug_principal(marca)


# ---------------------------------------------------------------------------
# Modo standalone (debug)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"[reclame_aqui] modo standalone - curl_cffi: {'ok' if _CURL_CFFI_OK else 'AUSENTE'}")
    marcas = _listar_marcas()
    health = _carregar_health()

    # Testa as primeiras marcas nao verificadas ainda
    fatia = _selecionar_fatia(marcas, health, MARCAS_POR_TICK * 2)
    if not fatia:
        fatia = marcas[:6]

    print(f"Testando {len(fatia)} marcas: {fatia}\n")
    for m in fatia:
        res = _buscar_reclame_aqui(m)
        st  = res.get("status", "?")
        if st == "encontrado":
            nota = res.get("nota")
            rep  = res.get("reputacao", "")
            tot  = res.get("total_reclamacoes")
            ra   = "[RA1000] " if res.get("ra1000") else ""
            print(f"  [OK] {m:30s} -> {ra}{rep} | nota={nota} | {tot} reclamacoes")
        else:
            print(f"  [--] {m:30s} -> {st}")
