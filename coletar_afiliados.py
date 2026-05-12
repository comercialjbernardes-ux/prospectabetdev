"""
coletar_afiliados.py — Coletor de programas de afiliados de bets legalizadas
============================================================================

Extrai, para cada site de bet regulamentada:
    - url_afiliado   : URL do programa de afiliados (própria ou white-label)
    - email_afiliado : email com prefixo de afiliação (partners@, affiliates@, ...)

API pública:
    coletar_afiliados(url_base, sessao) -> (url_afiliado, email_afiliado, status)

Reutiliza toda a infraestrutura de scraping do módulo `coletar_bets`
(buscar_html, extrair_emails_do_html, Playwright thread-local, robots,
curl_cffi, etc.). Nada é duplicado — apenas configurado com heurísticas
específicas do domínio de afiliação.

Status possíveis:
    encontrado_completo  — url E email achados
    encontrado_url       — só url (página não continha email de afiliação)
    encontrado_email     — só email (via footer/home, sem URL dedicada)
    nao_encontrado       — nada foi achado
    erro_conexao         — nenhuma página baixou
    sem_url              — url_base vazia
    bloqueado_robots     — robots.txt bloqueou acesso
"""

from __future__ import annotations

import logging
import re
import time
import random
from urllib.parse import urljoin, urlparse

# BeautifulSoup é importado de forma lazy dentro de _descobrir_candidatos_url()
# para evitar falha de importação quando bs4 não está instalado.

# Logger compartilhado com o módulo principal
logger = logging.getLogger("coletar_bets")

# ---------------------------------------------------------------------------
# Paths tentados diretamente (subpáginas internas do próprio domínio da bet)
# ---------------------------------------------------------------------------
SUBPAGINAS_AFILIADOS = [
    # PT-BR
    "/afiliados", "/afiliados.html", "/afiliados.php",
    "/afiliado", "/programa-de-afiliados", "/programa-afiliados",
    "/seja-afiliado", "/seja-um-afiliado",
    "/parceiros", "/parceria", "/parcerias", "/seja-parceiro",
    "/agentes",
    "/marketing",
    # EN
    "/affiliates", "/affiliate", "/affiliate-program", "/affiliate-programme",
    "/partners", "/partner", "/partner-program", "/partner-programme",
    "/agents",
    "/b2b",
    # Prefixos de idioma
    "/pt/afiliados", "/pt/parceiros",
    "/pt-br/afiliados", "/pt-br/parceiros",
    "/en/affiliates", "/en/partners",
]

# ---------------------------------------------------------------------------
# Palavras-chave para identificar âncoras de afiliados em qualquer página
# ---------------------------------------------------------------------------
_PALAVRAS_ANCORA_AFILIADOS = (
    "afiliado", "afiliados",
    "affiliate", "affiliates",
    "partner", "partners",
    "parceiro", "parceiros", "parceria", "parcerias",
    "programa de afiliados", "programa afiliados",
    "affiliate program", "partner program",
    "seja um parceiro", "seja parceiro", "seja afiliado", "seja um afiliado",
    "agente", "agentes", "agents",
    "b2b",
)

# ---------------------------------------------------------------------------
# Redes de afiliados conhecidas — whitelabels/plataformas do setor.
# Um href apontando pra um desses domínios é sinal forte de programa de
# afiliados hospedado.
# ---------------------------------------------------------------------------
REDES_AFILIADOS_CONHECIDAS = (
    "income-access.com",
    "netrefer.com",
    "myaffiliates.com",
    "affsource.com",
    "smartico.ai",
    "affilka.com",
    "scaleo.io",
    "trackier.com",
    "mediatech-solutions.com",
    "cellxpert.com",
    "igamingaffiliates.com",
    "betaffiliates.com.br",
    "goldenpartners.com",
    "nomini-partners.com",
)

# Sub-domínios típicos de programas próprios ("partners.<brand>", "afiliados.<brand>")
_SUBDOMINIOS_AFILIADOS = (
    "partners", "partner", "affiliate", "affiliates",
    "afiliados", "afiliado", "parceiros", "b2b", "agents",
)

# ---------------------------------------------------------------------------
# Emails — prefixos preferenciais e bloqueados
# ---------------------------------------------------------------------------
EMAILS_AFILIADOS_PREFERIDOS = (
    "afiliados", "afiliado",
    "affiliates", "affiliate",
    "partners", "partner",
    "parceria", "parcerias", "parceiros",
    "b2b",
    "agentes", "agents",
    "marketing",
    "comercial",
)

# Prefixos que NUNCA devem ser tratados como email de afiliação
EMAILS_AFILIADOS_BLOQUEADOS = (
    "denuncia", "denuncias", "ouvidoria",
    "juridico", "legal", "compliance",
    "lgpd", "dpo", "privacidade", "privacy",
    "abuse", "postmaster", "webmaster",
    "noreply", "no-reply", "donotreply",
    "security", "seguranca", "fraude", "antifraude",
)

# Domínios de rede de afiliados cujos emails são de suporte da ferramenta,
# não da bet — devem ser rejeitados como email_afiliado final.
_DOMINIOS_REDE_IGNORAR_EMAIL = tuple(
    d.lower() for d in REDES_AFILIADOS_CONHECIDAS
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalizar_texto(s: str) -> str:
    """Lowercase simples; sufuciente p/ comparação de âncoras."""
    return (s or "").lower().strip()


def _email_e_preferido_afiliado(email: str) -> bool:
    if "@" not in email:
        return False
    local = email.split("@", 1)[0].lower()
    return any(local == p or local.startswith(p) for p in EMAILS_AFILIADOS_PREFERIDOS)


def _email_e_bloqueado_afiliado(email: str) -> bool:
    if "@" not in email:
        return True
    local, _, dominio = email.partition("@")
    local = local.lower()
    dominio = dominio.lower()
    if any(local.startswith(p) for p in EMAILS_AFILIADOS_BLOQUEADOS):
        return True
    # Email do próprio provedor da rede white-label (ex.: support@income-access.com)
    if any(dominio == d or dominio.endswith("." + d) for d in _DOMINIOS_REDE_IGNORAR_EMAIL):
        return True
    return False


def _href_aponta_pra_rede_conhecida(href_abs: str) -> bool:
    try:
        netloc = urlparse(href_abs).netloc.lower()
    except Exception:
        return False
    if not netloc:
        return False
    return any(netloc == d or netloc.endswith("." + d) for d in REDES_AFILIADOS_CONHECIDAS)


def _path_parece_afiliado(path: str) -> bool:
    """True se o path contém indicativo claro de afiliados."""
    p = (path or "").lower()
    tokens = (
        "afiliado", "afiliados",
        "affiliate", "affiliates",
        "partner", "partners",
        "parceiro", "parceria",
        "seja-afiliado", "seja-parceiro",
        "programa-de-afiliados", "programa-afiliados",
        "b2b", "agentes", "agents",
    )
    return any(t in p for t in tokens)


def _subdominio_parece_afiliado(netloc: str, netloc_base: str) -> bool:
    """True se netloc é subdomínio do tipo partners.brand.com / afiliados.brand.com."""
    from coletar_bets import _mesmo_dominio_raiz  # lazy para evitar ciclo

    netloc = (netloc or "").lower()
    if not netloc:
        return False
    if not _mesmo_dominio_raiz(netloc, netloc_base):
        return False
    parte_inicial = netloc.split(".", 1)[0]
    if parte_inicial.startswith("www."):
        parte_inicial = parte_inicial[4:]
    return parte_inicial in _SUBDOMINIOS_AFILIADOS


def _score_candidato_url(href_abs: str, texto_ancora: str, netloc_base: str) -> int:
    """
    Pontua o candidato. Threshold de aceitação: score >= 2.

    +3  rede de afiliados conhecida
    +3  subdomínio do tipo partners./afiliados./b2b.
    +2  path com 'afiliado', 'affiliate', 'partner'...
    +2  texto da âncora é explicitamente de afiliação
    """
    score = 0
    try:
        p = urlparse(href_abs)
    except Exception:
        return 0
    if _href_aponta_pra_rede_conhecida(href_abs):
        score += 3
    if _subdominio_parece_afiliado(p.netloc, netloc_base):
        score += 3
    if _path_parece_afiliado(p.path):
        score += 2
    txt = _normalizar_texto(texto_ancora)
    if txt and any(p in txt for p in _PALAVRAS_ANCORA_AFILIADOS):
        score += 2
    return score


def _descobrir_candidatos_url(html: str, url_base: str) -> list[tuple[str, int]]:
    """
    Percorre TODOS os <a href> do HTML e retorna candidatos ordenados por score
    descrescente. Retorna [(url_absoluta, score), ...] com score >= 2.
    """
    if not html or not url_base:
        return []
    try:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            # Fallback: sem bs4, faz busca simples de hrefs via regex
            import re as _re
            netloc_base = urlparse(url_base).netloc.lower()
            encontrados: dict[str, int] = {}
            for m in _re.finditer(r'href=["\']([^"\']+)["\']', html, _re.IGNORECASE):
                href = m.group(1).strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    continue
                try:
                    absoluta = urljoin(url_base, href)
                except Exception:
                    continue
                parsed = urlparse(absoluta)
                if parsed.scheme not in ("http", "https"):
                    continue
                absoluta = parsed._replace(fragment="").geturl().rstrip("/")
                s = _score_candidato_url(absoluta, "", netloc_base)
                if s >= 2:
                    if absoluta not in encontrados or s > encontrados[absoluta]:
                        encontrados[absoluta] = s
            return sorted(encontrados.items(), key=lambda kv: kv[1], reverse=True)
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    netloc_base = urlparse(url_base).netloc.lower()
    encontrados: dict[str, int] = {}

    for tag in soup.find_all("a", href=True):
        href = (tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            absoluta = urljoin(url_base, href)
        except Exception:
            continue
        parsed = urlparse(absoluta)
        if parsed.scheme not in ("http", "https"):
            continue
        absoluta = parsed._replace(fragment="").geturl().rstrip("/")
        if not absoluta:
            continue
        texto = tag.get_text(" ", strip=True) or ""
        s = _score_candidato_url(absoluta, texto, netloc_base)
        if s >= 2:
            if absoluta not in encontrados or s > encontrados[absoluta]:
                encontrados[absoluta] = s

    return sorted(encontrados.items(), key=lambda kv: kv[1], reverse=True)


def _escolher_melhor_email(candidatos: list[str]) -> str:
    """Filtra bloqueados, prioriza preferenciais."""
    validos = [e for e in candidatos if not _email_e_bloqueado_afiliado(e)]
    if not validos:
        return ""
    preferidos = [e for e in validos if _email_e_preferido_afiliado(e)]
    return preferidos[0] if preferidos else validos[0]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def coletar_afiliados(url_base: str, sessao) -> tuple[str, str, str]:
    """
    Retorna (url_afiliado, email_afiliado, status).

    Ver docstring do módulo para os valores possíveis de status.
    """
    if not url_base:
        return "", "", "sem_url"

    # Lazy imports — evita ciclo e reutiliza infra do módulo principal
    from coletar_bets import (
        buscar_html,
        buscar_html_com_js,
        extrair_emails_do_html,
        verificar_robots,
        PLAYWRIGHT_DISPONIVEL,
    )

    if not verificar_robots(url_base, "/"):
        return "", "", "bloqueado_robots"

    acessou_ao_menos_uma = False
    url_afiliado: str = ""
    candidatos_email: list[str] = []

    # ── Fase 1a — baixa a home e procura âncoras de afiliados ───────────
    html_home = buscar_html(url_base, sessao)
    candidatos_url_scored: list[tuple[str, int]] = []
    if html_home:
        acessou_ao_menos_uma = True
        candidatos_url_scored = _descobrir_candidatos_url(html_home, url_base)
        logger.debug(
            f"[afiliados][{url_base}] {len(candidatos_url_scored)} candidato(s) de URL "
            f"via home"
        )
        # Também coleta emails já presentes na home — fallback p/ status
        # "encontrado_email" se nunca acharmos URL.
        for e in extrair_emails_do_html(html_home, url_base):
            if e not in candidatos_email:
                candidatos_email.append(e)

    # Primeiro candidato URL com score alto já é nosso winner provisório
    if candidatos_url_scored:
        url_afiliado = candidatos_url_scored[0][0]

    # ── Fase 1a.5 — Playwright na home se site é SPA e não achamos nada ─
    # (só vale a pena se home baixou mas não tinha candidato — indica JS-render)
    if not url_afiliado and html_home and PLAYWRIGHT_DISPONIVEL:
        html_low_home = html_home.lower()
        if not any(k in html_low_home for k in ("afiliado", "affiliate",
                                                 "partner", "parceiro")):
            logger.debug(
                f"[afiliados][{url_base}] home sem sinais — Playwright na home"
            )
            html_home_js = buscar_html_com_js(url_base)
            if html_home_js:
                acessou_ao_menos_uma = True
                candidatos_url_scored = _descobrir_candidatos_url(html_home_js, url_base)
                if candidatos_url_scored:
                    url_afiliado = candidatos_url_scored[0][0]
                    logger.debug(
                        f"[afiliados][{url_base}] url via Playwright home: {url_afiliado}"
                    )
                for e in extrair_emails_do_html(html_home_js, url_base):
                    if e not in candidatos_email:
                        candidatos_email.append(e)

    # ── Fase 1b — tenta subpaths internos diretos se nenhum candidato forte ─
    if not url_afiliado:
        for sub in SUBPAGINAS_AFILIADOS:
            candidato_url = (url_base.rstrip("/") + sub)
            caminho = urlparse(candidato_url).path or "/"
            if not verificar_robots(url_base, caminho):
                continue
            html_sub = buscar_html(candidato_url, sessao)
            if html_sub is None:
                continue
            acessou_ao_menos_uma = True
            # Se a página baixou E tem sinal de afiliados no conteúdo, aceitamos
            # Teste leve: HTML contém alguma palavra-chave ou é página "real"
            html_low = html_sub.lower()
            if any(k in html_low for k in ("afiliado", "affiliate", "partner",
                                            "parceiro", "parceria", "agentes")):
                url_afiliado = candidato_url
                logger.debug(
                    f"[afiliados][{url_base}] url_afiliado via subpath fixo: {candidato_url}"
                )
                # Extrai email já dessa página
                for e in extrair_emails_do_html(html_sub, candidato_url):
                    if e not in candidatos_email:
                        candidatos_email.append(e)
                break
            time.sleep(random.uniform(0.2, 0.5))

    # ── Fase 2 — baixa a página de afiliados e extrai email dedicado ─────
    email_da_pagina_afiliados = ""
    if url_afiliado:
        # Para URLs externas (redes white-label), ainda tentamos baixar,
        # mas robots do domínio externo não é nosso — respeitamos mesmo assim.
        parsed_af = urlparse(url_afiliado)
        path_af = parsed_af.path or "/"
        origem_af = f"{parsed_af.scheme}://{parsed_af.netloc}"
        # robots relativo ao host de destino
        if verificar_robots(origem_af, path_af):
            html_af = buscar_html(url_afiliado, sessao)
            if html_af:
                acessou_ao_menos_uma = True
                emails_pag = extrair_emails_do_html(html_af, url_afiliado)
                # Priorizamos emails preferenciais da página
                preferidos = [e for e in emails_pag
                              if _email_e_preferido_afiliado(e)
                              and not _email_e_bloqueado_afiliado(e)]
                if preferidos:
                    email_da_pagina_afiliados = preferidos[0]
                else:
                    # aceita qualquer não-bloqueado
                    nao_bloq = [e for e in emails_pag if not _email_e_bloqueado_afiliado(e)]
                    if nao_bloq:
                        email_da_pagina_afiliados = nao_bloq[0]
                # Acumula para fallback global também
                for e in emails_pag:
                    if e not in candidatos_email:
                        candidatos_email.append(e)

                # ── Fase 3 — Playwright se a página renderizou vazia/sem sinal ─
                if (not email_da_pagina_afiliados
                        and PLAYWRIGHT_DISPONIVEL
                        and (not html_af.strip() or len(html_af) < 2000)):
                    logger.debug(
                        f"[afiliados][{url_base}] Playwright fallback em {url_afiliado}"
                    )
                    html_js = buscar_html_com_js(url_afiliado)
                    if html_js:
                        emails_js = extrair_emails_do_html(html_js, url_afiliado)
                        preferidos_js = [e for e in emails_js
                                         if _email_e_preferido_afiliado(e)
                                         and not _email_e_bloqueado_afiliado(e)]
                        if preferidos_js:
                            email_da_pagina_afiliados = preferidos_js[0]
                        else:
                            nao_bloq_js = [e for e in emails_js
                                           if not _email_e_bloqueado_afiliado(e)]
                            if nao_bloq_js:
                                email_da_pagina_afiliados = nao_bloq_js[0]
                        for e in emails_js:
                            if e not in candidatos_email:
                                candidatos_email.append(e)

    # ── Fallback final de email: melhor de todos os candidatos acumulados ─
    email_final = email_da_pagina_afiliados or _escolher_melhor_email(candidatos_email)

    # ── Determina status ─────────────────────────────────────────────────
    if url_afiliado and email_final:
        logger.info(
            f"[afiliados][{url_base}] encontrado_completo url={url_afiliado} email={email_final}"
        )
        return url_afiliado, email_final, "encontrado_completo"
    if url_afiliado:
        logger.info(f"[afiliados][{url_base}] encontrado_url url={url_afiliado}")
        return url_afiliado, "", "encontrado_url"
    if email_final and _email_e_preferido_afiliado(email_final):
        logger.info(f"[afiliados][{url_base}] encontrado_email email={email_final}")
        return "", email_final, "encontrado_email"
    if not acessou_ao_menos_uma:
        logger.debug(f"[afiliados][{url_base}] erro_conexao")
        return "", "", "erro_conexao"
    logger.debug(f"[afiliados][{url_base}] nao_encontrado")
    return "", "", "nao_encontrado"


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import urllib3
    import requests as _requests

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Garante que o logger tenha handler (ao rodar standalone)
    from coletar_bets import configurar_logging
    configurar_logging()
    logger.setLevel(logging.DEBUG)

    alvos = [
        "https://betano.bet.br",
        "https://kto.bet.br",
        "https://betfair.bet.br",
    ]

    sessao = _requests.Session()
    print("=" * 70)
    print("  SMOKE TESTS — coletar_afiliados")
    print("=" * 70)

    for u in alvos:
        print(f"\n>>> {u}")
        try:
            url_af, email_af, status = coletar_afiliados(u, sessao)
        except Exception as e:
            print(f"    ERRO EXCEÇÃO: {e!r}")
            continue
        print(f"    status       : {status}")
        print(f"    url_afiliado : {url_af!r}")
        print(f"    email_afilia.: {email_af!r}")

    # Encerra Playwright se tiver sido iniciado
    try:
        from coletar_bets import fechar_browser_playwright
        fechar_browser_playwright()
    except Exception:
        pass
