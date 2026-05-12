"""
app.py â€” Dashboard Flask para visualizaÃ§Ã£o de bets regulamentadas
================================================================
Uso:
    python _start_server.py      (Waitress, produÃ§Ã£o)
    python app.py                (Flask dev, porta 5000)
    Acesse: http://127.0.0.1:5002

EdiÃ§Ã£o manual:
    POST /api/editar  â†’  overrides.json  â†’  audit_log.jsonl

PaginaÃ§Ã£o server-side (opcional):
    GET /api/dados?pagina=1&limite=50   â†’ retorna envelope {dados, total, ...}
    GET /api/dados                      â†’ retorna lista completa (retrocompat.)
"""

import csv
import io
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request
import json_store
from logging_config import get_logger

logger = get_logger(__name__)

# Rate limiting (etapa 1.4)
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _RATE_LIMIT_DISPONIVEL = True
except ImportError:
    Limiter = None  # type: ignore
    get_remote_address = lambda: "127.0.0.1"  # type: ignore
    _RATE_LIMIT_DISPONIVEL = False

import url_health
import csv_sync

try:
    import afiliados_health
    _AFILIADOS_HEALTH_DISPONIVEL = True
except ImportError:
    _AFILIADOS_HEALTH_DISPONIVEL = False

try:
    import reclame_aqui_health as _ra_health
    _RA_HEALTH_DISPONIVEL = True
except ImportError:
    _ra_health = None  # type: ignore
    _RA_HEALTH_DISPONIVEL = False

# MÃ³dulos auxiliares â€” top-level (antes eram importados lazy dentro de funÃ§Ãµes)
try:
    import stats_snapshot
    _STATS_SNAPSHOT_DISPONIVEL = True
except ImportError:
    stats_snapshot = None  # type: ignore
    _STATS_SNAPSHOT_DISPONIVEL = False

try:
    import notificacoes
    _NOTIFICACOES_DISPONIVEL = True
except ImportError:
    notificacoes = None  # type: ignore
    _NOTIFICACOES_DISPONIVEL = False

try:
    import ai_chat
    _AI_CHAT_DISPONIVEL = True
except ImportError:
    ai_chat = None  # type: ignore
    _AI_CHAT_DISPONIVEL = False

try:
    import analise_grupos
    import analise_anomalias
    _ANALISE_DISPONIVEL = True
except ImportError:
    analise_grupos = None    # type: ignore
    analise_anomalias = None  # type: ignore
    _ANALISE_DISPONIVEL = False

# Playwright disponÃ­vel?
try:
    from playwright.sync_api import sync_playwright as _pw  # noqa: F401
    _PLAYWRIGHT_DISPONIVEL = True
except ImportError:
    _PLAYWRIGHT_DISPONIVEL = False

app = Flask(__name__)

# Rate limiter â€” protege endpoints sensÃ­veis de abuso/DoS (etapa 1.4)
if _RATE_LIMIT_DISPONIVEL:
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],           # sem limite global; apenas em rotas marcadas
        storage_uri="memory://",     # storage in-memory (suficiente p/ instÃ¢ncia Ãºnica)
        headers_enabled=True,        # adiciona X-RateLimit-* headers
    )
else:
    limiter = None  # type: ignore
    logger.warning("Flask-Limiter nÃ£o instalado â€” rate limiting desabilitado")

# MÃ³dulos extraÃ­dos (etapa 2)
import data_manager
import audit

# Re-export de constantes para compatibilidade (testes e cÃ³digo legado)
ARQUIVO_JSON      = data_manager.ARQUIVO_JSON
ARQUIVO_CSV       = data_manager.ARQUIVO_CSV
ARQUIVO_OVERRIDES = data_manager.ARQUIVO_OVERRIDES
ARQUIVO_AUDIT     = audit.ARQUIVO_AUDIT
CAMPOS_EDITAVEIS  = data_manager.CAMPOS_EDITAVEIS

# Cache de stats (KPIs) â€” TTL curto
_STATS_CACHE_TTL   = 5.0        # segundos

# ValidaÃ§Ã£o de campos
_CAMPO_MAX_TAMANHO = 2_000       # caracteres
_CNPJ_SODIGITOS_RE = re.compile(r"^\d{14}$")

# ---------------------------------------------------------------------------
# Estado global â€” agora vive em data_manager. Os shims abaixo preservam a API
# usada por routes e testes legados.
# ---------------------------------------------------------------------------

_stats_cache: dict = {"data": None, "ts": 0.0}
_stats_lock  = Lock()


def _snapshot_dados() -> list[dict]:
    """Retorna cÃ³pia thread-safe dos dados (delega para data_manager)."""
    return data_manager.dados_snapshot()


def _invalidar_cache_stats() -> None:
    with _stats_lock:
        _stats_cache["ts"] = 0.0


def _invalidar_cache_health() -> None:
    """Compat shim â€” delega para data_manager."""
    data_manager.invalidar_cache_health()


# ---------------------------------------------------------------------------
# ValidaÃ§Ãµes de input
# ---------------------------------------------------------------------------

def _validar_cnpj_formato(cnpj: str) -> bool:
    """Aceita CNPJ com ou sem mÃ¡scara; valida que tenha 14 dÃ­gitos."""
    return bool(_CNPJ_SODIGITOS_RE.match(re.sub(r"\D", "", cnpj)))


def _validar_url_segura(url: str) -> bool:
    """Garante que a URL usa http/https e tem netloc nÃ£o-vazio."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False



# ---------------------------------------------------------------------------
# Audit log + Overrides + Carregamento de dados + Merges de health
# Todo o estado e a logica vivem em data_manager.py e audit.py.
# Os shims abaixo preservam a API legada (rotas e testes).
# ---------------------------------------------------------------------------

_STATUS_INATIVO       = data_manager.STATUS_INATIVO
_STATUS_ERRO          = _STATUS_INATIVO  # alias de compatibilidade
_STATUS_AFILIADOS_SIM = data_manager.STATUS_AFILIADOS_SIM
_STATUS_AFILIADOS_NAO = data_manager.STATUS_AFILIADOS_NAO

# Re-export para testes / codigo legado
_display_afiliados            = data_manager._display_afiliados
_aplicar_overrides            = data_manager.aplicar_overrides
_carregar_overrides           = data_manager.carregar_overrides
_salvar_overrides             = data_manager.salvar_overrides
_carregar_dados               = data_manager.carregar_dados
_aplicar_afiliados_health     = data_manager.aplicar_afiliados_health
_registrar_auditoria          = audit.registrar
_rotacionar_audit_log         = audit.rotacionar


def _aplicar_url_health(registros: list[dict]) -> None:
    data_manager.aplicar_url_health(registros, url_health)


def _aplicar_reclame_aqui_health(registros: list[dict]) -> None:
    data_manager.aplicar_reclame_aqui_health(registros, _ra_health if _RA_HEALTH_DISPONIVEL else None)


def recarregar_dados() -> None:
    '''Recarrega tudo do disco. Delega para data_manager.recarregar.'''
    data_manager.recarregar(
        url_health_module=url_health,
        ra_module=_ra_health if _RA_HEALTH_DISPONIVEL else None,
        stats_snapshot_module=stats_snapshot if _STATS_SNAPSHOT_DISPONIVEL else None,
    )
    _invalidar_cache_stats()



# ---------------------------------------------------------------------------
# Helpers de filtro (reutilizados por /api/dados paginado e /api/exportar)
# ---------------------------------------------------------------------------

_STATUS_FALHOU = {"erro_conexao", "bloqueado_robots", "sem_url"}


def _aplicar_filtros_query(registros: list[dict], params: dict) -> list[dict]:
    """Filtra registros pelos query params opcionais (mesma lÃ³gica do frontend)."""
    marca      = (params.get("marca") or "").lower().strip()
    status     = params.get("status", "")
    afiliados  = params.get("afiliados", "")
    porte      = params.get("porte", "")
    situacao   = params.get("situacao", "")
    uf         = (params.get("uf") or "").upper()
    municipio  = params.get("municipio", "")
    dt_ini     = params.get("data_inicio", "")
    dt_fim     = params.get("data_fim", "")
    saude_url  = params.get("saude_url", "")
    # Health Score mínimo (etapa 3.3)
    score_min_raw = params.get("score_min", "")
    try:
        score_min = int(score_min_raw) if score_min_raw not in ("", None) else None
    except ValueError:
        score_min = None

    resultado = []
    for r in registros:
        if marca and marca not in (r.get("marca") or "").lower():
            continue
        if status:
            if status == "com_email":
                if not r.get("email_contato"):
                    continue
            elif status == "sem_email":
                if r.get("email_contato"):
                    continue
            elif status == "falhou":
                if r.get("status") not in _STATUS_FALHOU:
                    continue
        if afiliados:
            disp = r.get("_afiliados_display") or "nao_encontrado"
            if afiliados == "com" and disp != "sim":      continue
            if afiliados == "sem" and disp != "nao":      continue
            if afiliados == "nd"  and disp != "nao_encontrado": continue
        if porte    and r.get("porte_empresa")     != porte:    continue
        if situacao and r.get("situacao_cadastral") != situacao: continue
        if uf       and (r.get("uf") or "").upper() != uf:       continue
        if municipio and r.get("municipio") != municipio:        continue
        if dt_ini and (r.get("data_coleta") or "")[:10] < dt_ini: continue
        if dt_fim and (r.get("data_coleta") or "")[:10] > dt_fim: continue
        if saude_url:
            st     = r.get("_url_health_status") or "desconhecido"
            _ONLINE = {"ok", "redirect", "bloqueado"}           # site acessÃ­vel p/ usuÃ¡rios reais
            _ERROS  = {"erro_http","erro_conexao","erro_ssl","erro_dns","timeout","erro"}
            if saude_url == "online"      and st not in _ONLINE: continue  # todas as acessÃ­veis
            if saude_url == "ok"          and st != "ok":         continue  # 200 direto
            if saude_url == "redirect"    and st != "redirect":   continue  # 30x
            if saude_url == "bloqueado"   and st != "bloqueado":  continue  # 4xx bots
            if saude_url == "erro"        and st not in _ERROS:   continue  # 5xx/offline
            if saude_url == "desconhecido" and st != "desconhecido": continue
        if score_min is not None and int(r.get("_health_score") or 0) < score_min:
            continue
        resultado.append(r)
    return resultado


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auditoria")
def auditoria():
    """Página de auditoria — delega para audit.ler_paginado()."""
    filtros = {
        "q":           (request.args.get("q") or "").strip(),
        "acao":        (request.args.get("acao") or "").strip().upper(),
        "campo":       (request.args.get("campo") or "").strip(),
        "data_inicio": (request.args.get("data_inicio") or "").strip(),
        "data_fim":    (request.args.get("data_fim") or "").strip(),
    }
    try:
        pagina_atual = max(1, int(request.args.get("page", 1)))
    except ValueError:
        pagina_atual = 1

    # Mapa CNPJ-só-dígitos → marca (para enriquecer eventos)
    snap = _snapshot_dados()
    cnpj_marca = {
        re.sub(r"\D", "", r.get("cnpj") or ""): r.get("marca", "")
        for r in snap if r.get("cnpj")
    }

    eventos, total_eventos, campos_disponiveis = audit.ler_paginado(
        filtros=filtros, pagina=pagina_atual, por_pagina=50, cnpj_marca_map=cnpj_marca,
    )
    total_paginas = max(1, (total_eventos + 49) // 50)
    pagina_atual = min(pagina_atual, total_paginas)

    return render_template(
        "auditoria.html",
        eventos=eventos,
        total_eventos=total_eventos,
        pagina_atual=pagina_atual,
        total_paginas=total_paginas,
        filtros=filtros,
        campos_disponiveis=campos_disponiveis,
    )


@app.route("/api/dados")
def api_dados():
    """
    Retorna registros com merges aplicados.

    Suporte a paginaÃ§Ã£o/filtro server-side (opcional):
        GET /api/dados?pagina=1&limite=50&marca=bet&uf=SP...
    Sem parÃ¢metros: retorna lista completa (retrocompat.).
    """
    snap = _snapshot_dados()
    _aplicar_url_health(snap)
    _aplicar_afiliados_health(snap)

    pagina = request.args.get("pagina", type=int)
    limite = request.args.get("limite", type=int)

    if pagina is not None and limite is not None:
        # Modo paginado com filtros server-side
        filtros = {k: v for k, v in request.args.items()
                   if k not in ("pagina", "limite")}
        filtrado = _aplicar_filtros_query(snap, filtros)
        inicio   = (pagina - 1) * limite
        fim      = inicio + limite
        return jsonify({
            "dados":         filtrado[inicio:fim],
            "total":         len(filtrado),
            "pagina":        pagina,
            "limite":        limite,
            "total_paginas": max(1, (len(filtrado) + limite - 1) // limite),
        })

    return jsonify(snap)


@app.route("/api/stats")
def api_stats():
    """KPI cards com cache de 5 segundos."""
    agora = time.monotonic()
    with _stats_lock:
        if _stats_cache["data"] is not None and (agora - _stats_cache["ts"]) < _STATS_CACHE_TTL:
            return jsonify(_stats_cache["data"])

    snap = _snapshot_dados()
    _aplicar_url_health(snap)
    _aplicar_afiliados_health(snap)

    total      = len(snap)
    com_email  = sum(1 for r in snap if r.get("status") in ("encontrado", "encontrado_js", "encontrado_manual"))
    sem_email  = sum(1 for r in snap if not r.get("email_contato"))
    com_afil   = sum(1 for r in snap if r.get("_afiliados_display") == "sim")
    afil_nao   = sum(1 for r in snap if r.get("_afiliados_display") == "nao")
    afil_nd    = sum(1 for r in snap if r.get("_afiliados_display") == "nao_encontrado")
    editados   = sum(1 for r in snap if r.get("_editado_manualmente"))

    # Ãšltima atualizaÃ§Ã£o: tenta data_coleta, cai para _enriquecido_em, cai para _adicionado_em
    def _melhor_data(r: dict) -> str:
        return (r.get("data_coleta") or r.get("_enriquecido_em") or r.get("_adicionado_em") or "")

    ultima = max((_melhor_data(r) for r in snap), default="")

    portes     = sorted({r.get("porte_empresa", "")    for r in snap if r.get("porte_empresa")})
    situacoes  = sorted({r.get("situacao_cadastral", "") for r in snap if r.get("situacao_cadastral")})
    ufs        = sorted({r.get("uf", "")               for r in snap if r.get("uf")})

    _URL_ONLINE     = {"ok", "redirect", "bloqueado"}   # site acessÃ­vel p/ usuÃ¡rios reais
    urls_online     = sum(1 for r in snap if r.get("_url_health_status") in _URL_ONLINE)
    urls_ok         = sum(1 for r in snap if r.get("_url_health_status") == "ok")
    urls_redirect   = sum(1 for r in snap if r.get("_url_health_status") == "redirect")
    urls_bloqueadas = sum(1 for r in snap if r.get("_url_health_status") == "bloqueado")
    urls_inativas   = sum(1 for r in snap if r.get("_url_inativa"))
    urls_desc       = sum(1 for r in snap if r.get("_url_health_status") == "desconhecido")
    # alias de retrocompat. (card usa urls_ativas â†’ agora = online total)
    urls_ativas     = urls_online

    ra_encontradas     = sum(1 for r in snap if r.get("_ra_status") == "encontrado")
    ra_nao_encontradas = sum(1 for r in snap if r.get("_ra_status") == "nao_encontrado")
    ra_pendentes       = sum(1 for r in snap if r.get("_ra_status", "desconhecido") == "desconhecido")

    sync_info = csv_sync.ler_status()

    data = {
        "total": total,
        "com_email": com_email, "sem_email": sem_email,
        "com_afiliados": com_afil, "afiliados_sim": com_afil,
        "afiliados_nao": afil_nao, "afiliados_desconhecido": afil_nd,
        "editados_manualmente": editados, "ultima_atualizacao": ultima,
        "portes": portes, "situacoes": situacoes, "ufs": ufs,
        "urls_ativas": urls_ativas,          # total online (ok + redirect + bloqueado)
        "urls_ok": urls_ok,                  # somente 200 OK direto
        "urls_redirect": urls_redirect,
        "urls_bloqueadas": urls_bloqueadas,
        "urls_inativas": urls_inativas, "urls_desconhecido": urls_desc,
        "ra_encontradas": ra_encontradas,
        "ra_nao_encontradas": ra_nao_encontradas,
        "ra_pendentes": ra_pendentes,
        "playwright_disponivel": _PLAYWRIGHT_DISPONIVEL,
        "csv_sync": {
            "ultimo_sync": sync_info.get("finalizado_em") or sync_info.get("iniciado_em"),
            "sucesso":     sync_info.get("sucesso", False),
            "adicionadas": len(sync_info.get("adicionadas", [])),
            "removidas":   len(sync_info.get("removidas", [])),
            "url_atualizada": len(sync_info.get("url_atualizada", [])),
        },
    }

    with _stats_lock:
        _stats_cache["data"] = data
        _stats_cache["ts"]   = time.monotonic()

    return jsonify(data)


@app.route("/api/municipios/<uf>")
def api_municipios(uf: str):
    municipios = sorted({
        r.get("municipio", "")
        for r in _snapshot_dados()
        if r.get("uf", "").upper() == uf.upper() and r.get("municipio")
    })
    return jsonify(municipios)


@app.route("/api/url-health")
def api_url_health():
    return jsonify(url_health.ler_health())


@app.route("/api/afiliados-health")
def api_afiliados_health():
    path = Path("dados/afiliados_health.json")
    if not path.exists():
        return jsonify({})
    try:
        return jsonify(json.loads(path.read_text("utf-8")))
    except Exception:
        return jsonify({})


@app.route("/api/url-health-worker-status")
def api_url_health_worker_status():
    """Retorna estado do worker de url_health e estatÃ­sticas do arquivo."""
    import threading
    worker_vivo = any(
        t.name == "url-health-worker" and t.is_alive()
        for t in threading.enumerate()
    )
    health = url_health.ler_health()
    from datetime import datetime as _dt
    agora = _dt.now()
    idades_min = []
    status_count: dict[str, int] = {}
    for info in health.values():
        st = info.get("status", "desconhecido")
        status_count[st] = status_count.get(st, 0) + 1
        t = info.get("checado_em", "")
        if t:
            try:
                idades_min.append((_dt.now() - _dt.fromisoformat(t)).total_seconds() / 60)
            except Exception:
                pass
    return jsonify({
        "worker_ativo":    worker_vivo,
        "total_urls":      len(health),
        "status_counts":   status_count,
        "idade_min_min":   round(min(idades_min), 1) if idades_min else None,
        "idade_max_min":   round(max(idades_min), 1) if idades_min else None,
        "idade_media_min": round(sum(idades_min) / len(idades_min), 1) if idades_min else None,
    })


@app.route("/api/force-health-recheck", methods=["POST"])
def api_force_health_recheck():
    """
    Apaga os timestamps de `checado_em` do url_health.json, forÃ§ando
    o worker a re-validar todas as URLs no prÃ³ximo tick.
    """
    try:
        health = url_health.ler_health()
        for info in health.values():
            info.pop("checado_em", None)
        import json_store as _js
        _js.salvar(Path("dados/url_health.json"), health)
        return jsonify({"ok": True, "urls_resetadas": len(health)})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/reclame-aqui-health")
def api_reclame_aqui_health():
    """Retorna o JSON completo de saÃºde do Reclame Aqui (chave=slug)."""
    path = Path("dados/reclame_aqui_health.json")
    if not path.exists():
        return jsonify({})
    try:
        return jsonify(json.loads(path.read_text("utf-8")))
    except Exception:
        return jsonify({})


@app.route("/api/csv-sync-status")
def api_csv_sync_status():
    return jsonify(csv_sync.ler_status())


@app.route("/api/csv-sync-agora", methods=["POST"])
def api_csv_sync_agora():
    resultado = csv_sync.sincronizar_uma_vez()
    if resultado.get("sucesso"):
        recarregar_dados()
    return jsonify(resultado)


@app.route("/api/recarregar", methods=["POST"])
def api_recarregar():
    recarregar_dados()
    return jsonify({"ok": True, "total": len(_snapshot_dados())})


@app.route("/api/audit-log")
def api_audit_log():
    """Retorna as Ãºltimas N entradas do audit log (padrÃ£o 200, mÃ¡x 1000)."""
    limite = min(int(request.args.get("limite", 200)), 1000)
    campo  = request.args.get("campo", "")
    cnpj   = request.args.get("cnpj", "")
    if not ARQUIVO_AUDIT.exists():
        return jsonify([])
    try:
        with open(ARQUIVO_AUDIT, encoding="utf-8") as fh:
            linhas = fh.readlines()
    except OSError:
        return jsonify([])
    entradas = []
    for linha in reversed(linhas):
        linha = linha.strip()
        if not linha:
            continue
        try:
            e = json.loads(linha)
        except json.JSONDecodeError:
            continue
        if campo and e.get("campo") != campo:
            continue
        if cnpj and e.get("cnpj") != cnpj:
            continue
        entradas.append(e)
        if len(entradas) >= limite:
            break
    return jsonify(entradas)


@app.route("/api/editar", methods=["POST"])
@(limiter.limit("10 per minute") if _RATE_LIMIT_DISPONIVEL else (lambda f: f))
def api_editar():
    """
    Edita um campo de um registro.

    Rate limit: 10 ediÃ§Ãµes/minuto por IP (etapa 1.4 â€” protege contra abuso/DoS).

    Body JSON:
        {"cnpj": "12345678000100", "campo": "email_contato", "valor": "x@y.com"}

    valor=null  â†’ reset (volta ao valor base)
    valor=""    â†’ deleÃ§Ã£o explÃ­cita (mascara o valor base)
    """
    payload = request.get_json(silent=True) or {}
    cnpj    = (payload.get("cnpj") or "").strip()
    campo   = (payload.get("campo") or "").strip()
    valor   = payload.get("valor")
    if isinstance(valor, str):
        valor = valor.strip()

    if not cnpj:
        return jsonify({"ok": False, "erro": "CNPJ ausente."}), 400

    # ValidaÃ§Ã£o de formato do CNPJ (apenas dÃ­gitos, 14 caracteres)
    if not _validar_cnpj_formato(cnpj):
        return jsonify({"ok": False, "erro": "CNPJ deve conter 14 dÃ­gitos numÃ©ricos."}), 400

    if campo not in CAMPOS_EDITAVEIS:
        return jsonify({
            "ok": False,
            "erro": f"Campo '{campo}' nÃ£o Ã© editÃ¡vel.",
            "editaveis": sorted(CAMPOS_EDITAVEIS),
        }), 400

    # ValidaÃ§Ã£o de tamanho mÃ¡ximo
    if isinstance(valor, str) and len(valor) > _CAMPO_MAX_TAMANHO:
        return jsonify({"ok": False, "erro": f"Valor excede {_CAMPO_MAX_TAMANHO} caracteres."}), 400

    # ValidaÃ§Ã£o de email
    if campo == "email_contato" and valor:
        if "@" not in valor or "." not in valor.split("@")[-1]:
            return jsonify({"ok": False, "erro": "Email invÃ¡lido."}), 400

    # ValidaÃ§Ã£o de URL (http/https obrigatÃ³rio)
    if campo in ("url", "url_afiliados") and valor:
        if not _validar_url_segura(valor):
            return jsonify({"ok": False, "erro": "URL deve comeÃ§ar com http:// ou https://."}), 400

    resetar = valor is None
    deletar = isinstance(valor, str) and valor == ""

    snap = _snapshot_dados()
    registro_atual = next((r for r in snap if (r.get("cnpj") or "").strip() == cnpj), None)
    valor_anterior = registro_atual.get(campo) if registro_atual else None

    # Atualiza override (data_manager cuida do lock + persistência)
    overrides = data_manager.carregar_overrides()
    reg_ov    = overrides.get(cnpj, {})
    if resetar:
        reg_ov.pop(campo, None)
    else:
        reg_ov[campo] = valor
    campos_reais = [k for k in reg_ov if not k.startswith("_")]
    if not campos_reais:
        overrides.pop(cnpj, None)
    else:
        reg_ov["_edited_at"] = datetime.now().isoformat(timespec="seconds")
        overrides[cnpj] = reg_ov
    data_manager.salvar_overrides(overrides)

    recarregar_dados()

    acao = "reset" if resetar else ("delete" if deletar else "edit")
    _registrar_auditoria(
        acao=acao, cnpj=cnpj, campo=campo,
        valor_anterior=valor_anterior,
        valor_novo=valor if not resetar else None,
        ip=request.remote_addr or "",
    )

    # Dispara notificaÃ§Ã£o se configurada
    if _NOTIFICACOES_DISPONIVEL:
        try:
            notificacoes.notificar_edicao(cnpj=cnpj, campo=campo,
                                          valor_anterior=valor_anterior, valor_novo=valor)
        except Exception:
            logger.exception("Falha ao disparar notificaÃ§Ã£o")

    atualizado = next(
        (r for r in _snapshot_dados() if (r.get("cnpj") or "").strip() == cnpj), None
    )
    return jsonify({"ok": True, "registro": atualizado})


# ---------------------------------------------------------------------------
# FASE 3 â€” Endpoints adicionais
# ---------------------------------------------------------------------------

@app.route("/api/duplicatas")
def api_duplicatas():
    """
    Retorna emails que aparecem em mais de um registro distinto.
    Ãštil para identificar dados genÃ©ricos (info@, contato@) compartilhados.
    """
    snap = _snapshot_dados()
    contagem: dict[str, list[str]] = {}
    for r in snap:
        email = (r.get("email_contato") or "").strip().lower()
        if not email:
            continue
        marca = r.get("marca") or r.get("cnpj") or "?"
        contagem.setdefault(email, []).append(marca)
    duplicatas = [
        {"email": email, "marcas": marcas, "qtd": len(marcas)}
        for email, marcas in contagem.items()
        if len(marcas) > 1
    ]
    duplicatas.sort(key=lambda x: -x["qtd"])
    return jsonify(duplicatas)


@app.route("/api/exportar")
def api_exportar():
    """Export server-side (CSV/XLSX) — delega para export.py."""
    import export
    fmt    = request.args.get("formato", "csv")
    filtros = {k: v for k, v in request.args.items() if k != "formato"}
    snap = _snapshot_dados()
    _aplicar_url_health(snap)
    _aplicar_afiliados_health(snap)
    dados_filtrados = _aplicar_filtros_query(snap, filtros) if filtros else snap
    return export.exportar(dados_filtrados, formato=fmt)


@app.route("/api/snapshots")
def api_snapshots():
    """Retorna histÃ³rico diÃ¡rio de KPIs para sparklines dinÃ¢micas."""
    if not _STATS_SNAPSHOT_DISPONIVEL:
        return jsonify([])
    try:
        return jsonify(stats_snapshot.ler_snapshots())
    except Exception:
        logger.exception("Falha ao ler snapshots")
        return jsonify([])


@app.route("/notificacoes")
def notificacoes_page():
    """Página de configuração de alertas/webhooks (etapa 4.4)."""
    return render_template("notificacoes.html")


@app.route("/api/notificacoes/config", methods=["GET", "POST"])
def api_notificacoes_config():
    """GET retorna config atual; POST atualiza."""
    if not _NOTIFICACOES_DISPONIVEL:
        return jsonify({"erro": "MÃ³dulo notificacoes indisponÃ­vel"}), 503
    try:
        if request.method == "POST":
            nova = request.get_json(silent=True) or {}
            notificacoes.salvar_config(nova)
            return jsonify({"ok": True, "config": notificacoes.ler_config()})
        return jsonify(notificacoes.ler_config())
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/notificacoes/teste", methods=["POST"])
def api_notificacoes_teste():
    """Dispara um webhook de teste com payload de exemplo."""
    if not _NOTIFICACOES_DISPONIVEL:
        return jsonify({"ok": False, "mensagem": "MÃ³dulo notificacoes indisponÃ­vel"}), 503
    try:
        ok, msg = notificacoes.disparar_teste()
        return jsonify({"ok": ok, "mensagem": msg})
    except Exception as e:
        return jsonify({"ok": False, "mensagem": str(e)}), 500


@app.route("/api/chat", methods=["POST"])
@(limiter.limit("20 per minute") if _RATE_LIMIT_DISPONIVEL else (lambda f: f))
def api_chat():
    """
    Chat AI sobre os dados (etapa 5).
    Body: {"pergunta": "...", "historico": [...optional]}
    Retorna: {"resposta": "markdown text", "tokens_input": N, "cache_read": N, ...}
    Rate limit: 20 req/min/IP.
    """
    if not _AI_CHAT_DISPONIVEL:
        return jsonify({
            "erro": "indisponivel",
            "resposta": "❌ Módulo ai_chat não foi carregado. Instale o pacote 'anthropic'.",
        }), 503
    if not ai_chat.disponivel():
        return jsonify({
            "erro": "sem_api_key",
            "resposta": "❌ ANTHROPIC_API_KEY não está configurada no servidor. "
                        "Configure a variável de ambiente para habilitar o chat.",
        }), 503

    body = request.get_json(silent=True) or {}
    pergunta = (body.get("pergunta") or "").strip()
    historico = body.get("historico") or []

    if not pergunta:
        return jsonify({"erro": "pergunta_vazia", "resposta": "Envie uma pergunta no campo 'pergunta'."}), 400
    if len(pergunta) > 2000:
        return jsonify({"erro": "pergunta_longa", "resposta": "Pergunta muito longa (máx 2000 caracteres)."}), 400
    if not isinstance(historico, list) or len(historico) > 20:
        return jsonify({"erro": "historico_invalido", "resposta": "Histórico inválido ou muito longo (máx 20 mensagens)."}), 400

    try:
        resultado = ai_chat.responder(pergunta=pergunta, historico=historico)
        return jsonify(resultado)
    except Exception as e:
        logger.exception("Erro em /api/chat")
        return jsonify({
            "erro": "erro_interno",
            "resposta": f"❌ Erro interno: {e}",
        }), 500


@app.route("/api/holdings")
def api_holdings():
    """Grupos empresariais por CNPJ raiz (etapa 6.1). Query: ?top=N (default 5)."""
    if not _ANALISE_DISPONIVEL:
        return jsonify({"holdings": [], "stats": {}})
    try:
        top = int(request.args.get("top", 5))
    except ValueError:
        top = 5
    snap = _snapshot_dados()
    holdings = analise_grupos.agrupar(snap)
    stats = analise_grupos.estatisticas_grupos(snap)
    return jsonify({
        "holdings": holdings[:top],
        "total_holdings": len(holdings),
        "stats": stats,
    })


@app.route("/api/anomalias")
def api_anomalias():
    """Painel de anomalias da semana (etapa 6.3)."""
    if not _ANALISE_DISPONIVEL:
        return jsonify({"erro": "Módulo de análise indisponível"}), 503
    snap = _snapshot_dados()
    return jsonify(analise_anomalias.resumo(snap))


@app.route("/health")
def health():
    """
    Health check agregado â€” pensado para monitoramento externo / alerting.

    Retorna:
      status: "ok" | "degraded" | "critical"
      workers: dict por worker â†’ {alive, file_age_seg, ultimo_check}
      ultima_recarga_dados: ISO timestamp
      uptime_segundos: segundos desde recarga inicial
    """
    import sys as _sys
    agora = time.time()

    workers_info = {}

    def _info_worker(nome: str, mod, arquivo_health: Path) -> dict:
        thread = getattr(mod, "_thread_ref", None) if mod else None
        alive  = bool(thread and thread.is_alive())
        idade  = None
        if arquivo_health.exists():
            try:
                idade = round(agora - arquivo_health.stat().st_mtime)
            except OSError:
                idade = None
        # Estado do circuit breaker (se o worker expÃµe estado_circuit_breaker)
        cb_estado = None
        if mod is not None:
            getter = getattr(mod, "estado_circuit_breaker", None)
            if callable(getter):
                try:
                    cb_estado = getter()
                except Exception:
                    cb_estado = None
        return {
            "alive":           alive,
            "arquivo_idade_s": idade,   # None se arquivo nÃ£o existe
            "arquivo_existe":  arquivo_health.exists(),
            "circuit_breaker": cb_estado,
        }

    workers_info["url_health"]     = _info_worker("url_health",     url_health,     Path("dados/url_health.json"))
    workers_info["csv_sync"]       = _info_worker("csv_sync",       csv_sync,       Path("dados/csv_sync_status.json"))
    workers_info["afiliados"]      = _info_worker("afiliados",      afiliados_health if _AFILIADOS_HEALTH_DISPONIVEL else None, Path("dados/afiliados_health.json"))
    workers_info["reclame_aqui"]   = _info_worker("reclame_aqui",   _ra_health      if _RA_HEALTH_DISPONIVEL        else None, Path("dados/reclame_aqui_health.json"))

    # Determina status agregado
    workers_essenciais = ["url_health", "csv_sync"]
    workers_secundarios = ["afiliados", "reclame_aqui"]

    essenciais_mortos  = sum(1 for k in workers_essenciais  if not workers_info[k]["alive"])
    secundarios_mortos = sum(1 for k in workers_secundarios if not workers_info[k]["alive"])

    # Arquivos health "velhos" (> 1h = 3600s) tambÃ©m indicam degradaÃ§Ã£o
    arquivos_velhos = sum(
        1 for k, info in workers_info.items()
        if info["alive"] and info["arquivo_idade_s"] is not None and info["arquivo_idade_s"] > 3600
    )

    # Algum circuit breaker aberto? (worker pausado por falhas seguidas)
    cb_abertos = sum(
        1 for info in workers_info.values()
        if info.get("circuit_breaker") and info["circuit_breaker"].get("em_pausa")
    )

    if essenciais_mortos > 0:
        status_agregado = "critical"
    elif secundarios_mortos > 0 or arquivos_velhos > 0 or cb_abertos > 0:
        status_agregado = "degraded"
    else:
        status_agregado = "ok"

    info = data_manager.info_recarga()
    ts = info["ultima_recarga_ts"]
    return jsonify({
        "status":                 status_agregado,
        "workers":                workers_info,
        "ultima_recarga_dados":   datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None,
        "segundos_desde_recarga": round(agora - ts) if ts else None,
        "total_registros":        info["total_registros"],
        "python":                 _sys.version.split()[0],
    }), (200 if status_agregado == "ok" else 503 if status_agregado == "critical" else 200)


@app.route("/api/sistema")
def api_sistema():
    """InformaÃ§Ãµes do ambiente (versÃµes, dependÃªncias disponÃ­veis)."""
    import sys
    deps = {}
    for lib in ("bs4", "waitress", "playwright", "openpyxl", "curl_cffi", "pandas"):
        try:
            m = __import__(lib)
            deps[lib] = getattr(m, "__version__", "ok")
        except ImportError:
            deps[lib] = None
    return jsonify({
        "python":              sys.version.split()[0],
        "flask":               __import__("flask").__version__,
        "playwright_disponivel": _PLAYWRIGHT_DISPONIVEL,
        "dependencias":        deps,
        "total_registros":     data_manager.info_recarga()["total_registros"],
        "arquivo_json_existe": ARQUIVO_JSON.exists(),
    })


# ---------------------------------------------------------------------------
# InicializaÃ§Ã£o
# ---------------------------------------------------------------------------

recarregar_dados()


def _deve_iniciar_worker() -> bool:
    if __name__ != "__main__":
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


if _deve_iniciar_worker():
    url_health.iniciar_worker()
    csv_sync.iniciar_worker()
    if _AFILIADOS_HEALTH_DISPONIVEL:
        afiliados_health.iniciar_worker()
    if _RA_HEALTH_DISPONIVEL:
        _ra_health.iniciar_worker()


if __name__ == "__main__":
    recarregar_dados()
    fonte = "JSON enriquecido" if ARQUIVO_JSON.exists() else "CSV básico"
    snap_dados = _snapshot_dados()
    n_ov  = sum(1 for r in snap_dados if r.get("_editado_manualmente"))
    print(f"\nDashboard iniciado — {len(snap_dados)} registros ({fonte})")
    if n_ov:
        print(f"  {n_ov} ediÃ§Ã£o(Ãµes) manual(is) aplicada(s)")
    print("Acesse: http://localhost:5000\n")
    app.run(debug=True, port=5000)
