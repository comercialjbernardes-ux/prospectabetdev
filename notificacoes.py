"""
notificacoes.py — Notificações via webhook (Slack, Discord, HTTP genérico)
==========================================================================
Config persistida em dados/notificacoes_config.json.

Exemplo de config:
    {
        "habilitado": true,
        "webhook_url": "https://hooks.slack.com/services/...",
        "eventos": ["edit", "delete", "reset"],
        "tipo": "slack"   // "slack" | "discord" | "json"
    }

API pública:
    notificacoes.notificar_edicao(cnpj, campo, valor_anterior, valor_novo)
    notificacoes.disparar_teste()  → (bool_ok, str_mensagem)
    notificacoes.ler_config()      → dict
    notificacoes.salvar_config(nova: dict)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Garante imports locais
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ARQUIVO_CONFIG = Path("dados/notificacoes_config.json")

_CONFIG_PADRAO = {
    "habilitado":   False,
    "webhook_url":  "",
    "tipo":         "json",       # "slack" | "discord" | "json"
    # Tipos de evento suportados:
    #   edit, delete, reset      — edições manuais via /api/editar (etapa 1.0)
    #   url_down                 — site caiu 3+ vezes em 24h (etapa 4.1)
    #   ra_score_drop            — nota Reclame Aqui caiu >=0.5 (etapa 4.2)
    #   bet_removed              — bet saiu da lista gov.br (etapa 4.3)
    "eventos":      ["edit", "delete", "url_down", "ra_score_drop", "bet_removed"],
    "timeout_seg":  5,
}

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Persistência de config
# ---------------------------------------------------------------------------

def ler_config() -> dict:
    """Retorna config mesclada com defaults (campos faltantes preenchidos)."""
    if not ARQUIVO_CONFIG.exists():
        return dict(_CONFIG_PADRAO)
    try:
        raw = json.loads(ARQUIVO_CONFIG.read_text("utf-8"))
    except Exception:
        return dict(_CONFIG_PADRAO)
    cfg = dict(_CONFIG_PADRAO)
    cfg.update({k: v for k, v in raw.items() if k in _CONFIG_PADRAO})
    return cfg


def salvar_config(nova: dict) -> None:
    """Persiste campos válidos de `nova` mesclados com a config atual."""
    cfg = ler_config()
    for k, v in nova.items():
        if k in _CONFIG_PADRAO:
            cfg[k] = v
    ARQUIVO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARQUIVO_CONFIG.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(ARQUIVO_CONFIG)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Formatação de payloads
# ---------------------------------------------------------------------------

def _payload_slack(titulo: str, campos: dict) -> dict:
    linhas = [f"*{titulo}*"]
    for k, v in campos.items():
        linhas.append(f"• `{k}`: {v}")
    return {"text": "\n".join(linhas)}


def _payload_discord(titulo: str, campos: dict) -> dict:
    desc = "\n".join(f"**{k}**: {v}" for k, v in campos.items())
    return {"embeds": [{"title": titulo, "description": desc, "color": 0x3B82F6}]}


def _payload_json(titulo: str, campos: dict) -> dict:
    return {"titulo": titulo, "ts": datetime.now().isoformat(timespec="seconds"), **campos}


def _construir_payload(cfg: dict, titulo: str, campos: dict) -> dict:
    tipo = cfg.get("tipo", "json")
    if tipo == "slack":
        return _payload_slack(titulo, campos)
    if tipo == "discord":
        return _payload_discord(titulo, campos)
    return _payload_json(titulo, campos)


# ---------------------------------------------------------------------------
# Envio de webhook
# ---------------------------------------------------------------------------

def _enviar_webhook(url: str, payload: dict, timeout: int = 5) -> tuple[bool, str]:
    """Faz POST HTTP com JSON. Retorna (ok, mensagem)."""
    try:
        import urllib.request as _req
        import urllib.error as _err
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req  = _req.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "ProspectorBets/1.0"},
            method="POST",
        )
        with _req.urlopen(req, timeout=timeout) as resp:
            status = resp.status
        if 200 <= status < 300:
            return True, f"HTTP {status}"
        return False, f"HTTP {status}"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def notificar_evento(tipo: str, titulo: str, campos: dict) -> None:
    """
    Dispara webhook em background se `tipo` está na lista de eventos configurados.
    API genérica usada pelos workers (url_down, ra_score_drop, bet_removed) e
    também internamente por `notificar_edicao`. Falhas são silenciosas.

    :param tipo:   identificador do evento (deve estar em cfg.eventos)
    :param titulo: título exibido no webhook
    :param campos: dict com pares chave-valor que vão para o corpo
    """
    def _tarefa() -> None:
        cfg = ler_config()
        if not cfg.get("habilitado"):
            return
        if tipo not in (cfg.get("eventos") or []):
            return
        url = (cfg.get("webhook_url") or "").strip()
        if not url:
            return
        # Garantia: campo `ts` sempre presente
        body = dict(campos)
        body.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        body.setdefault("tipo", tipo)
        payload = _construir_payload(cfg, titulo, body)
        _enviar_webhook(url, payload, timeout=cfg.get("timeout_seg", 5))

    threading.Thread(target=_tarefa, daemon=True).start()


def notificar_edicao(
    cnpj: str,
    campo: str,
    valor_anterior,
    valor_novo,
    acao: str = "edit",
) -> None:
    """Compat: chamada original do /api/editar. Internamente usa notificar_evento."""
    def _tarefa():
        cfg = ler_config()
        if not cfg.get("habilitado"):
            return
        if acao not in (cfg.get("eventos") or []):
            return
        url = (cfg.get("webhook_url") or "").strip()
        if not url:
            return

        titulo  = f"Prospector Bets — edição ({campo})"
        campos  = {
            "cnpj":       cnpj,
            "campo":      campo,
            "anterior":   str(valor_anterior) if valor_anterior is not None else "—",
            "novo":       str(valor_novo)      if valor_novo      is not None else "—",
            "acao":       acao,
            "ts":         datetime.now().isoformat(timespec="seconds"),
        }
        payload = _construir_payload(cfg, titulo, campos)
        _enviar_webhook(url, payload, timeout=cfg.get("timeout_seg", 5))

    threading.Thread(target=_tarefa, daemon=True).start()


def disparar_teste() -> tuple[bool, str]:
    """Envia webhook de teste síncrono. Retorna (ok, mensagem)."""
    cfg = ler_config()
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        return False, "Webhook URL não configurada."
    titulo = "Prospector Bets — teste de webhook"
    campos = {
        "mensagem": "Este é um teste enviado pelo dashboard.",
        "ts":       datetime.now().isoformat(timespec="seconds"),
    }
    payload = _construir_payload(cfg, titulo, campos)
    return _enviar_webhook(url, payload, timeout=cfg.get("timeout_seg", 5))
