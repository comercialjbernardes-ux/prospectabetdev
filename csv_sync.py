"""
csv_sync.py — Sincronização automática com o CSV oficial do gov.br
===================================================================

Worker em thread daemon que, a cada SYNC_INTERVALO_HORAS, busca a planilha
mais recente publicada na página da Secretaria de Prêmios e Apostas, compara
com `dados/bets_enriquecidas.json` e reporta mudanças:

- Bets adicionadas → acrescentadas automaticamente (status inicial vazio)
- Bets removidas   → marcadas com `_removido_do_csv=true` (não deletadas)
- Bets alteradas   → URL base atualizada se divergente

O estado da última sincronização é salvo em `dados/csv_sync_status.json`.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import json_store

# ---------------------------------------------------------------------------
# Parâmetros
# ---------------------------------------------------------------------------

SYNC_INTERVALO_HORAS = 6          # Sincroniza a cada 6 horas
SYNC_PRIMEIRO_DELAY_SEG = 120     # Espera 2 min após boot antes do 1º sync

URL_PAGINA_LISTA = (
    "https://www.gov.br/fazenda/pt-br/composicao/orgaos/"
    "secretaria-de-premios-e-apostas/lista-de-empresas"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ARQUIVO_DADOS = Path("dados/bets_enriquecidas.json")
ARQUIVO_STATUS = Path("dados/csv_sync_status.json")

_lock = threading.Lock()
_thread_ref: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Scraping do índice — descobre CSV mais recente
# ---------------------------------------------------------------------------


_REGEX_CSV = re.compile(
    r'href="([^"]*planilha[^"]*\.csv)"', re.IGNORECASE
)
_REGEX_DATA_NO_NOME = re.compile(r"(\d{2})-(\d{2})-(\d{4})")


def detectar_csv_mais_recente() -> str | None:
    """
    Faz scraping da página de lista-de-empresas e retorna a URL do CSV
    mais recente baseado na data no nome do arquivo.
    """
    try:
        resp = requests.get(
            URL_PAGINA_LISTA,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[csv_sync] falha ao buscar índice: {e}")
        return None

    hrefs = _REGEX_CSV.findall(resp.text)
    if not hrefs:
        return None

    # Ranqueia por data no nome do arquivo (DD-MM-YYYY → YYYYMMDD)
    candidatos = []
    for h in hrefs:
        m = _REGEX_DATA_NO_NOME.search(h)
        chave = int(f"{m[3]}{m[2]}{m[1]}") if m else 0
        url_abs = h if h.startswith("http") else \
            "https://www.gov.br" + (h if h.startswith("/") else "/" + h)
        candidatos.append((chave, url_abs))

    candidatos.sort(reverse=True)
    return candidatos[0][1] if candidatos else None


# ---------------------------------------------------------------------------
# Download + parse do CSV
# ---------------------------------------------------------------------------


def baixar_e_parsear_csv(url: str) -> list[dict] | None:
    """
    Retorna lista de dicts (uma por bet) ou None em erro.
    Reutiliza a lógica de parsing do pipeline — forward-fill de CNPJ e razão
    social quando uma bet tem múltiplas marcas/domínios em linhas sucessivas.
    """
    import re
    import unicodedata

    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=40,
        )
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
    except requests.RequestException as e:
        print(f"[csv_sync] falha ao baixar CSV: {e}")
        return None

    try:
        df = pd.read_csv(
            io.StringIO(resp.text),
            sep=";",
            skiprows=1,
            dtype=str,
            engine="python",
        )
    except Exception as e:
        print(f"[csv_sync] falha ao parsear CSV: {e}")
        return None

    # Slug das colunas (igual ao coletar_bets.py)
    def _slug(s: str) -> str:
        s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
        s = re.sub(r"\s+", "_", s.strip().lower())
        return re.sub(r"[^\w]", "_", s)

    df.columns = [_slug(c) for c in df.columns]

    mapa = {
        "denominacao_social_da_empresa": "razao_social",
        "razao_social": "razao_social",
        "empresa": "razao_social",
        "marcas": "marca",
        "marca": "marca",
        "nome_fantasia": "marca",
        "cnpj": "cnpj",
        "dominios": "url",
        "dominio": "url",
        "url": "url",
        "site": "url",
    }
    df = df.rename(columns={c: mapa[c] for c in df.columns if c in mapa})
    for col in ("razao_social", "cnpj", "marca", "url"):
        if col not in df.columns:
            df[col] = ""

    df = df.dropna(how="all").reset_index(drop=True)

    # Forward-fill razao_social e cnpj — uma bet com múltiplas marcas ocupa
    # várias linhas com essas colunas em branco após a primeira
    df["razao_social"] = df["razao_social"].replace(r"^\s*$", pd.NA, regex=True).ffill()
    df["cnpj"] = df["cnpj"].replace(r"^\s*$", pd.NA, regex=True).ffill()
    df["marca"] = df["marca"].fillna("").str.strip()
    df["url"] = df["url"].fillna("").str.strip()
    df["razao_social"] = df["razao_social"].fillna("").str.strip()
    df["cnpj"] = df["cnpj"].fillna("").str.strip()

    # Normaliza URLs: adiciona https:// se faltar
    def _norm_url(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        if not u.startswith(("http://", "https://")):
            u = "https://" + u.lstrip("/")
        return u.rstrip("/")

    df["url"] = df["url"].apply(_norm_url)
    df = df[df["url"].str.startswith("http")].reset_index(drop=True)
    df = df.drop_duplicates(subset="url").reset_index(drop=True)

    registros = []
    for _, row in df.iterrows():
        cnpj = row["cnpj"]
        if not cnpj or not row["marca"]:
            continue
        registros.append({
            "cnpj": cnpj,
            "marca": row["marca"],
            "razao_social": row["razao_social"],
            "url": row["url"],
        })
    return registros


# ---------------------------------------------------------------------------
# Reconciliação com bets_enriquecidas.json
# ---------------------------------------------------------------------------


def _carregar_base() -> list[dict]:
    return json_store.ler(ARQUIVO_DADOS, default=[]) or []


def _salvar_base(dados: list[dict]) -> None:
    json_store.salvar(ARQUIVO_DADOS, dados)


def _salvar_status(info: dict) -> None:
    json_store.salvar(ARQUIVO_STATUS, info, criar_backup=False)


def ler_status() -> dict:
    return json_store.ler(ARQUIVO_STATUS, default={}) or {}


def sincronizar_uma_vez() -> dict:
    """
    Executa um ciclo completo de sync e retorna info de resultado.
    """
    inicio = datetime.now()
    info = {
        "iniciado_em": inicio.isoformat(timespec="seconds"),
        "url_csv": None,
        "sucesso": False,
        "adicionadas": [],
        "removidas": [],
        "url_atualizada": [],
        "erro": None,
    }

    url_csv = detectar_csv_mais_recente()
    info["url_csv"] = url_csv
    if not url_csv:
        info["erro"] = "CSV mais recente não foi detectado no índice"
        _salvar_status(info)
        return info

    novos = baixar_e_parsear_csv(url_csv)
    if novos is None:
        info["erro"] = "Falha ao baixar/parsear CSV"
        _salvar_status(info)
        return info

    def _chave(r: dict) -> str:
        """
        Chave composta CNPJ+URL — uma empresa pode ter várias marcas/URLs.
        Precisa diferenciar cada bet (linha do CSV), não cada empresa.
        """
        cnpj = (r.get("cnpj") or "").strip()
        url = (r.get("url") or "").strip().rstrip("/")
        return f"{cnpj}|{url}"

    with _lock:
        base = _carregar_base()
        base_idx = {_chave(r): r for r in base}
        novos_idx = {_chave(r): r for r in novos}

        # Adicionadas: no CSV novo mas não na base
        for k, r in novos_idx.items():
            if not r.get("cnpj") or not r.get("url"):
                continue
            if k not in base_idx:
                registro = {
                    **r,
                    "status": "",
                    "email_contato": "",
                    "data_coleta": "",
                    "_origem": "csv_sync",
                    "_adicionado_em": inicio.isoformat(timespec="seconds"),
                }
                base.append(registro)
                info["adicionadas"].append({
                    "cnpj": r["cnpj"], "marca": r["marca"], "url": r["url"],
                })

        # Removidas: na base mas não no CSV novo
        for k, existente in base_idx.items():
            if not existente.get("cnpj") or not existente.get("url"):
                continue
            if k not in novos_idx:
                if not existente.get("_removido_do_csv"):
                    existente["_removido_do_csv"] = True
                    existente["_removido_em"] = inicio.isoformat(timespec="seconds")
                    info["removidas"].append({
                        "cnpj": existente.get("cnpj", ""),
                        "marca": existente.get("marca", ""),
                        "url": existente.get("url", ""),
                    })
                    # Dispara alerta (etapa 4.3)
                    try:
                        from notificacoes import notificar_evento
                        notificar_evento(
                            tipo="bet_removed",
                            titulo=f"🚫 Bet removida da lista gov.br — {existente.get('marca', '')}",
                            campos={
                                "marca":       existente.get("marca", ""),
                                "cnpj":        existente.get("cnpj", ""),
                                "url":         existente.get("url", ""),
                                "removido_em": existente.get("_removido_em"),
                            },
                        )
                    except Exception:
                        pass  # falha silenciosa — não bloqueia o sync
            else:
                # Reativa se voltou
                if existente.get("_removido_do_csv"):
                    existente.pop("_removido_do_csv", None)
                    existente.pop("_removido_em", None)

        # Só grava se houve mudanças
        if info["adicionadas"] or info["removidas"] or info["url_atualizada"]:
            _salvar_base(base)

    info["sucesso"] = True
    info["total_csv"] = len(novos_idx)
    info["total_base"] = len(base_idx) + len(info["adicionadas"])
    info["finalizado_em"] = datetime.now().isoformat(timespec="seconds")
    _salvar_status(info)

    print(
        f"[csv_sync] OK · +{len(info['adicionadas'])} "
        f"-{len(info['removidas'])} ~{len(info['url_atualizada'])} "
        f"(total CSV: {info['total_csv']})"
    )
    return info


def _loop() -> None:
    print(f"[csv_sync] worker iniciado — intervalo={SYNC_INTERVALO_HORAS}h")
    time.sleep(SYNC_PRIMEIRO_DELAY_SEG)
    while True:
        try:
            sincronizar_uma_vez()
        except Exception as e:
            print(f"[csv_sync] erro inesperado: {e}")
        time.sleep(SYNC_INTERVALO_HORAS * 3600)


def iniciar_worker() -> None:
    global _thread_ref
    if _thread_ref and _thread_ref.is_alive():
        return
    t = threading.Thread(target=_loop, name="csv-sync-worker", daemon=True)
    t.start()
    _thread_ref = t


if __name__ == "__main__":
    # Modo standalone: executa um sync único e sai
    resultado = sincronizar_uma_vez()
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
