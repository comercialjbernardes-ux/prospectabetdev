"""
afiliados_health.py — Worker de detecção automática de programas de afiliados
=============================================================================

Thread daemon que, a cada TICK_SEGUNDOS, testa URLS_POR_TICK bets em busca
de programa de afiliados, salvando em dados/afiliados_health.json.

Reutiliza 100% da lógica de detecção de coletar_afiliados.py.

Status possíveis (retornados por coletar_afiliados):
  encontrado_completo  — URL + email de afiliados encontrados
  encontrado_url       — só URL do programa
  encontrado_email     — só email de afiliados
  nao_encontrado       — nenhum programa detectado
  erro_conexao         — falha ao acessar o site
  sem_url              — registro sem URL
  bloqueado_robots     — robots.txt bloqueou

`_afiliado_detectado` = True para todos os status "encontrado_*"
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------

TICK_SEGUNDOS = 60          # Intervalo entre ticks
URLS_POR_TICK = 5           # Bets verificadas por tick
WORKERS = 3                 # Threads paralelas por tick
INTERVALO_RE_CHECK = 300    # Não re-checa a mesma bet antes de 5 min

ARQUIVO_DADOS = Path("dados/bets_enriquecidas.json")
ARQUIVO_AFILIADOS = Path("dados/afiliados_health.json")

_STATUS_DETECTADO = {"encontrado_completo", "encontrado_url", "encontrado_email"}

_lock = threading.Lock()
_thread_ref: threading.Thread | None = None


# ---------------------------------------------------------------------------
# I/O thread-safe
# ---------------------------------------------------------------------------


def _carregar_afiliados() -> dict:
    if not ARQUIVO_AFILIADOS.exists():
        return {}
    try:
        with open(ARQUIVO_AFILIADOS, encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _salvar_afiliados(data: dict) -> None:
    ARQUIVO_AFILIADOS.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARQUIVO_AFILIADOS.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(ARQUIVO_AFILIADOS)


def _listar_urls() -> list[str]:
    if not ARQUIVO_DADOS.exists():
        return []
    try:
        with open(ARQUIVO_DADOS, encoding="utf-8") as f:
            base = json.load(f)
        return sorted({
            (r.get("url") or "").strip()
            for r in base
            if (r.get("url") or "").strip().startswith(("http://", "https://"))
        })
    except (json.JSONDecodeError, OSError):
        return []


# ---------------------------------------------------------------------------
# Detecção de afiliados para uma URL
# ---------------------------------------------------------------------------


def _detectar_afiliados(url: str) -> dict:
    """
    Chama coletar_afiliados() e normaliza o resultado para o schema do worker.
    Cria uma Session por chamada — cada worker thread tem a sua própria.
    """
    try:
        from coletar_afiliados import coletar_afiliados
        # Session não é thread-safe para compartilhamento entre threads
        # mas é segura para uso exclusivo dentro de um único executor task
        with requests.Session() as sessao:
            url_af, email_af, status = coletar_afiliados(url, sessao)
    except Exception as e:
        return {
            "detectado": False,
            "status": "erro_conexao",
            "url_afiliado": "",
            "email_afiliado": "",
            "checado_em": datetime.now().isoformat(timespec="seconds"),
            "erro": str(e)[:200],
        }

    return {
        "detectado": status in _STATUS_DETECTADO,
        "status": status,
        "url_afiliado": url_af or "",
        "email_afiliado": email_af or "",
        "checado_em": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Seleção de fatia (prioriza nunca-checadas e mais antigas)
# ---------------------------------------------------------------------------


def _selecionar_fatia(urls: list[str], afiliados: dict, n: int) -> list[str]:
    agora = datetime.now()

    def _idade(u: str) -> float:
        info = afiliados.get(u)
        if not info or "checado_em" not in info:
            return float("inf")
        try:
            t = datetime.fromisoformat(info["checado_em"])
            return (agora - t).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    candidatas = [
        (u, _idade(u)) for u in urls
        if _idade(u) == float("inf") or _idade(u) >= INTERVALO_RE_CHECK
    ]
    candidatas.sort(key=lambda x: -x[1])
    return [u for u, _ in candidatas[:n]]


# ---------------------------------------------------------------------------
# Tick e loop
# ---------------------------------------------------------------------------


def _tick() -> tuple[int, int]:
    urls = _listar_urls()
    with _lock:
        afiliados = _carregar_afiliados()

    fatia = _selecionar_fatia(urls, afiliados, URLS_POR_TICK)
    if not fatia:
        return 0, 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        resultados = list(zip(fatia, pool.map(_detectar_afiliados, fatia)))

    with _lock:
        afiliados = _carregar_afiliados()
        for url, res in resultados:
            afiliados[url] = res
        # Limpa entradas órfãs
        urls_ativas = set(urls)
        for u in list(afiliados.keys()):
            if u not in urls_ativas:
                afiliados.pop(u, None)
        _salvar_afiliados(afiliados)

    detectados = sum(1 for _, r in resultados if r.get("detectado"))
    return len(fatia), detectados


def _loop() -> None:
    print(f"[afiliados_health] worker iniciado — tick={TICK_SEGUNDOS}s · "
          f"fatia={URLS_POR_TICK} · workers={WORKERS}")
    while True:
        try:
            n, det = _tick()
            if n:
                print(f"[afiliados_health] tick: {n} bets checadas, {det} com afiliados")
        except Exception as e:
            print(f"[afiliados_health] erro no tick: {e}")
        time.sleep(TICK_SEGUNDOS)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def iniciar_worker() -> None:
    """Inicia thread daemon (idempotente)."""
    global _thread_ref
    if _thread_ref and _thread_ref.is_alive():
        return
    t = threading.Thread(
        target=_loop, name="afiliados-health-worker", daemon=True
    )
    t.start()
    _thread_ref = t


def ler_afiliados() -> dict:
    """Leitura thread-safe do arquivo de detecção."""
    with _lock:
        return _carregar_afiliados()


if __name__ == "__main__":
    # Modo standalone: executa um tick único para debug
    print("Executando tick de detecção de afiliados...")
    urls = _listar_urls()
    print(f"{len(urls)} URLs carregadas. Checando primeiras {URLS_POR_TICK}...")
    afiliados = _carregar_afiliados()
    fatia = _selecionar_fatia(urls, afiliados, URLS_POR_TICK)
    for url in fatia:
        res = _detectar_afiliados(url)
        sinal = "🟢" if res["detectado"] else "🔴"
        print(f"  {sinal} {url} → {res['status']} {res.get('url_afiliado','')}")
