"""
coletar_bets.py — Coletor de emails de bets legalizadas no Brasil
=================================================================
Fonte oficial: Ministério da Fazenda / Secretaria de Prêmios e Apostas
URL do CSV: https://www.gov.br/fazenda/pt-br/composicao/orgaos/secretaria-de-premios-e-apostas/lista-de-empresas

Uso:
    # Baixa o CSV automaticamente e processa:
    python coletar_bets.py

    # Usa um CSV local já baixado:
    python coletar_bets.py --csv meu_arquivo.csv

    # Limita a N empresas (útil para testes):
    python coletar_bets.py --limite 10

    # Força reprocessar mesmo empresas já no checkpoint:
    python coletar_bets.py --reiniciar

Requisitos:
    pip install -r requirements.txt

Saída gerada:
    bets_com_emails.csv   — base enriquecida com emails
    relatorio.txt         — resumo da coleta
    checkpoint.json       — progresso salvo (permite retomada)
    coleta.log            — log completo da execução
"""

import argparse
import csv
import json
import logging
import os
import re
import threading
import time
import random
import unicodedata
import urllib.robotparser
from datetime import datetime
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
try:
    from bs4 import BeautifulSoup as BeautifulSoup
    _BS4_OK = True
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment,misc]
    _BS4_OK = False

# curl_cffi impersona TLS fingerprint do Chrome — bypassa Cloudflare básico
try:
    from curl_cffi import requests as cffi_requests
    CFFI_DISPONIVEL = True
except ImportError:
    CFFI_DISPONIVEL = False

# Playwright — fallback para sites 100% JS-rendered
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_DISPONIVEL = True
except ImportError:
    PLAYWRIGHT_DISPONIVEL = False

# ---------------------------------------------------------------------------
# Configurações globais
# ---------------------------------------------------------------------------

URL_CSV_OFICIAL = (
    "https://www.gov.br/fazenda/pt-br/composicao/orgaos/"
    "secretaria-de-premios-e-apostas/lista-de-empresas/"
    "planilha-de-autorizacoes-02-04-2026.csv"
)

ARQUIVO_SAIDA = "bets_com_emails.csv"
ARQUIVO_RELATORIO = "relatorio.txt"
ARQUIVO_CHECKPOINT = "checkpoint.json"
ARQUIVO_LOG = "coleta.log"

# Sub-páginas tentadas quando a home não tem email — cobre padrões reais
# em bets BR/internacionais. A ordem prioriza contato direto e central de ajuda
# (onde 90% dos emails reais estão escondidos).
SUBPAGINAS = [
    # Contato direto
    "/contato", "/contact", "/fale-conosco", "/fale_conosco",
    # Suporte
    "/suporte", "/support", "/atendimento", "/sac",
    # Ajuda — CRÍTICO: 90% dos emails estão aqui
    "/ajuda", "/help", "/central-de-ajuda", "/centro-de-ajuda",
    "/help-center", "/helpcenter", "/centraldeajuda",
    # FAQ
    "/faq", "/perguntas-frequentes", "/duvidas",
    # Institucional
    "/sobre", "/about", "/sobre-nos", "/about-us",
    # Páginas legais (têm contato LGPD — filtrado — mas também contato geral)
    "/termos", "/termos-de-uso", "/termos-e-condicoes",
    "/terms", "/terms-of-use", "/terms-and-conditions",
    "/privacidade", "/politica-de-privacidade", "/privacy", "/privacy-policy",
    # Com prefixos de idioma comuns em bets internacionais
    "/pt/help", "/pt/contact", "/pt/ajuda", "/pt/suporte",
    "/en/help", "/en/contact", "/en/support",
    "/pt-br/ajuda", "/pt-br/contato", "/pt-br/help",
]

# Padrão regex para emails válidos
REGEX_EMAIL = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Regex complementar para emails ofuscados (usar apenas em texto bruto,
# nunca em atributos HTML). Ex.: "contato [at] site [dot] com [dot] br".
# Permite TLDs compostos (.com.br, .co.uk, .gov.br) aceitando até 3 grupos
# "(dot|ponto|.) + sufixo" após a primeira parte do domínio.
REGEX_EMAIL_OFUSCADO = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*(?:\[at\]|\(at\)|\[arroba\]|\(arroba\)|@)\s*"
    r"[a-zA-Z0-9.\-]+"
    r"(?:\s*(?:\[dot\]|\(dot\)|\[ponto\]|\.)\s*[a-zA-Z]{2,}){1,3}",
    re.IGNORECASE,
)

# Palavras-chave usadas na descoberta dinâmica de links úteis na home
_PALAVRAS_LINKS_UTEIS = (
    "ajuda", "help", "suporte", "support", "contato", "contact",
    "atendimento", "sac", "faq", "duvida", "fale", "central", "assistencia",
)


def _normalizar(s: str) -> str:
    """Normaliza string removendo acentos e convertendo para minúsculo."""
    if s is None:
        return ""
    return (
        unicodedata.normalize("NFKD", str(s))
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )


def _desofuscar(s: str) -> str:
    """Substitui ofuscações comuns ([at], [dot], etc.) pelos caracteres reais."""
    if not s:
        return s
    out = s
    # Order matters: substitui "at/arroba" por @ e "dot/ponto" por .
    for pad in (r"\s*\[\s*at\s*\]\s*", r"\s*\(\s*at\s*\)\s*",
                r"\s*\[\s*arroba\s*\]\s*", r"\s*\(\s*arroba\s*\)\s*"):
        out = re.sub(pad, "@", out, flags=re.IGNORECASE)
    for pad in (r"\s*\[\s*dot\s*\]\s*", r"\s*\(\s*dot\s*\)\s*",
                r"\s*\[\s*ponto\s*\]\s*", r"\s*\(\s*ponto\s*\)\s*"):
        out = re.sub(pad, ".", out, flags=re.IGNORECASE)
    return out


def _mesmo_dominio_raiz(netloc_a: str, netloc_b: str) -> bool:
    """
    Compara dois netlocs aceitando subdomínios legítimos.
    Ex.: help.betway.com e betway.com → True.
    Remove porta e www. antes de comparar.
    """
    def _limpar(n: str) -> str:
        n = (n or "").lower().split(":")[0]
        return n[4:] if n.startswith("www.") else n
    a, b = _limpar(netloc_a), _limpar(netloc_b)
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _extrair_emails_jsonld(obj, acumulador: set[str]) -> None:
    """
    Percorre recursivamente um objeto JSON-LD (dict/list) coletando emails.
    - Chaves "email"/"contactEmail" com string contendo "@" -> adiciona direto.
    - Qualquer string contendo "@" e "." -> extrai via REGEX_EMAIL.
    """
    if isinstance(obj, dict):
        for chave, valor in obj.items():
            if isinstance(valor, str):
                chave_low = str(chave).lower()
                if chave_low in ("email", "contactemail") and "@" in valor:
                    for e in REGEX_EMAIL.findall(valor):
                        acumulador.add(e.lower())
                    # também aceita valor direto se não casou regex (ex.: "foo@bar.com")
                    if "@" in valor and "." in valor:
                        for e in REGEX_EMAIL.findall(valor):
                            acumulador.add(e.lower())
                elif "@" in valor and "." in valor:
                    for e in REGEX_EMAIL.findall(valor):
                        acumulador.add(e.lower())
            else:
                _extrair_emails_jsonld(valor, acumulador)
    elif isinstance(obj, list):
        for item in obj:
            _extrair_emails_jsonld(item, acumulador)


def _descobrir_via_sitemap(url_base: str, sessao: requests.Session) -> list[str]:
    """
    Baixa sitemap.xml (ou sitemap_index.xml como fallback), expande índices,
    filtra URLs cujo path contenha palavras-chave de _PALAVRAS_LINKS_UTEIS,
    e retorna até 15 URLs absolutas do mesmo domínio.

    Timeout curto por sitemap (5s), sem retries agressivos.
    Respeita robots.txt — se robots bloquear o sitemap, retorna [].
    """
    if not url_base:
        return []

    parsed_base = urlparse(url_base)
    origem = f"{parsed_base.scheme}://{parsed_base.netloc}"
    base_netloc = parsed_base.netloc.lower()

    # robots check
    if not verificar_robots(url_base, "/sitemap.xml"):
        return []

    candidatos_sitemap = [
        f"{origem}/sitemap.xml",
        f"{origem}/sitemap_index.xml",
    ]

    loc_regex = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)

    locs_coletados: list[str] = []
    sitemaps_visitados: set[str] = set()

    def _baixar_sitemap(url_sm: str, profundidade: int = 0) -> None:
        if profundidade > 1 or url_sm in sitemaps_visitados:
            return
        sitemaps_visitados.add(url_sm)
        try:
            resp = sessao.get(
                url_sm, headers=_headers_aleatorios(), timeout=5,
                allow_redirects=True, verify=False,
            )
            if resp.status_code >= 400 or not resp.text:
                return
            conteudo = resp.text
        except Exception:
            return

        locs_encontrados = loc_regex.findall(conteudo)
        for loc in locs_encontrados:
            loc = loc.strip()
            if not loc:
                continue
            # É sub-sitemap? (termina em .xml ou contém "sitemap" no path)
            if loc.lower().endswith(".xml"):
                _baixar_sitemap(loc, profundidade + 1)
            else:
                locs_coletados.append(loc)

    for sm in candidatos_sitemap:
        _baixar_sitemap(sm)
        if locs_coletados:
            break  # se o primeiro já deu resultado, não precisa do _index

    # Filtra: mesmo domínio raiz + palavra-chave no path
    resultado: list[str] = []
    vistos: set[str] = set()
    for loc in locs_coletados:
        try:
            p = urlparse(loc)
        except Exception:
            continue
        if p.scheme not in ("http", "https"):
            continue
        if not _mesmo_dominio_raiz(p.netloc, base_netloc):
            continue
        path_norm = _normalizar(p.path)
        if not any(pkw in path_norm for pkw in _PALAVRAS_LINKS_UTEIS):
            continue
        norm = loc.rstrip("/")
        if norm in vistos:
            continue
        vistos.add(norm)
        resultado.append(loc)
        if len(resultado) >= 15:
            break
    return resultado


def _descobrir_links_uteis(html: str, url_base: str) -> list[str]:
    """
    Extrai até 20 URLs absolutas do mesmo domínio (ou subdomínio) da home
    cujo texto visível ou href contenha palavras-chave de suporte/ajuda/contato.
    Aceita subdomínios legítimos (ex.: help.betway.com quando base é betway.com).
    Ignora âncoras (#), mailto: e tel:.
    """
    if not html or not url_base:
        return []
    if not _BS4_OK:
        return []  # sem bs4, retorna vazio — sem impacto no daemon de afiliados
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    base_netloc = urlparse(url_base).netloc.lower()
    encontrados: list[str] = []
    vistos: set[str] = set()

    for tag in soup.find_all("a", href=True):
        href = (tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        texto = tag.get_text(" ", strip=True) or ""
        alvo = _normalizar(texto) + " " + _normalizar(href)
        if not any(p in alvo for p in _PALAVRAS_LINKS_UTEIS):
            continue
        try:
            absoluta = urljoin(url_base, href)
        except Exception:
            continue
        parsed = urlparse(absoluta)
        if parsed.scheme not in ("http", "https"):
            continue
        # Remove fragmento; mantém query (alguns SPAs usam ?page=help)
        absoluta = parsed._replace(fragment="").geturl().rstrip("/")
        if not absoluta:
            continue
        # Aceita mesmo domínio OU subdomínio legítimo (help.bet.com, ajuda.bet.com)
        if not _mesmo_dominio_raiz(parsed.netloc, base_netloc):
            continue
        if absoluta in vistos:
            continue
        vistos.add(absoluta)
        encontrados.append(absoluta)
        if len(encontrados) >= 20:
            break
    return encontrados

# Prefixos de emails descartados por serem genéricos/falsos ou não-comerciais
EMAILS_IGNORADOS = (
    # Sistema / automação
    "noreply", "no-reply", "donotreply", "mailer", "bounce", "postmaster",
    "webmaster", "root@",
    # Placeholders / testes
    "example", "test", "teste", "info@sentry", "info@example",
    # Jurídico / compliance / ouvidoria (não são contato comercial)
    # Emails RFC não têm acentos — basta a versão sem acento para casar tudo
    "denuncia", "denuncias", "ouvidoria", "juridico",
    "legal", "compliance", "lgpd", "dpo", "privacidade", "privacy",
    # Segurança / fraude / abuso
    "abuse", "security", "seguranca", "fraude", "antifraude", "anti-fraude",
    # ATO / atendimento-ao-consumidor formal (preferir "sac"/"atendimento")
    "ato", "atendimentoaoconsumidor", "atendimento-ao-consumidor",
)

# Prefixos preferenciais — contatos comerciais verdadeiros.
# Usados para reordenar a lista de emails encontrados (preferidos primeiro)
# sem descartar os demais, e para decisão de early-exit em coletar_email_empresa.
EMAILS_PREFERIDOS = (
    "contato", "contact", "sac", "atendimento",
    "suporte", "support", "help", "ajuda",
    "faleconosco", "fale-conosco", "comercial",
    "hello", "ola", "olá", "oi", "info",
)


def _e_preferido(email: str) -> bool:
    """Indica se o email tem prefixo comercial preferencial."""
    local = email.split("@", 1)[0].lower()
    return any(local == p or local.startswith(p) for p in EMAILS_PREFERIDOS)

# Pool de User-Agents rotativos para reduzir fingerprinting
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

# Headers base — User-Agent substituído por _headers_aleatorios() em cada requisição
HEADERS = {
    "User-Agent": USER_AGENTS[0],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def _headers_aleatorios() -> dict:
    """Retorna headers com User-Agent aleatório do pool."""
    return {**HEADERS, "User-Agent": random.choice(USER_AGENTS)}


# Lock para acesso thread-safe ao cache de robots e ao checkpoint
_ROBOTS_LOCK = threading.Lock()
_CHECKPOINT_LOCK = threading.Lock()

TIMEOUT = 10        # segundos por requisição
DELAY_MIN = 1.0     # segundos mínimos entre requisições
DELAY_MAX = 3.0     # segundos máximos entre requisições

# ---------------------------------------------------------------------------
# Logging — terminal colorido + arquivo
# ---------------------------------------------------------------------------

def configurar_logging() -> logging.Logger:
    """
    Configura logger com saída simultânea para terminal e arquivo .log.
    Idempotente: não adiciona handlers se já existirem, evitando logs duplicados
    quando o módulo é importado por pipeline.py e configurar_logging() é chamado
    novamente ali.
    """
    logger = logging.getLogger("coletar_bets")
    if logger.handlers:
        return logger  # já configurado — retorna sem adicionar handlers duplos

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handler de arquivo (nível DEBUG — tudo registrado)
    fh = logging.FileHandler(ARQUIVO_LOG, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    # Handler de terminal (nível INFO — só o essencial)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# Logger configurado uma única vez no nível de módulo.
# Chamadas subsequentes a configurar_logging() verificam se handlers já existem
# para evitar duplicação quando o módulo é importado por pipeline.py.
logger = configurar_logging()

# ---------------------------------------------------------------------------
# Checkpoint — permite retomar coleta interrompida
# ---------------------------------------------------------------------------

def carregar_checkpoint() -> dict:
    """Carrega o progresso salvo anteriormente (se existir)."""
    if Path(ARQUIVO_CHECKPOINT).exists():
        with open(ARQUIVO_CHECKPOINT, encoding="utf-8") as f:
            dados = json.load(f)
        logger.info(f"Checkpoint carregado — {len(dados)} registros já processados.")
        return dados
    return {}


def salvar_checkpoint(checkpoint: dict) -> None:
    """Persiste o progresso atual em disco (thread-safe)."""
    with _CHECKPOINT_LOCK:
        with open(ARQUIVO_CHECKPOINT, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Etapa 1 — Carregar CSV oficial
# ---------------------------------------------------------------------------

def baixar_csv(url: str) -> str:
    """
    Tenta baixar o CSV oficial do Ministério da Fazenda.

    Retorna o conteúdo bruto como string ou levanta RuntimeError se falhar.
    """
    logger.info(f"Baixando CSV oficial de: {url}")
    try:
        resp = requests.get(url, headers=_headers_aleatorios(), timeout=30)
        resp.raise_for_status()
        # Força encoding correto para CSV brasileiro (frequentemente latin-1)
        resp.encoding = resp.apparent_encoding or "utf-8"
        logger.info(f"CSV baixado com sucesso ({len(resp.content):,} bytes).")
        return resp.text
    except requests.RequestException as e:
        raise RuntimeError(
            f"Falha ao baixar CSV: {e}\n"
            "Baixe o arquivo manualmente em:\n"
            "  https://www.gov.br/fazenda/pt-br/composicao/orgaos/"
            "secretaria-de-premios-e-apostas/lista-de-empresas\n"
            "e passe o caminho com --csv <arquivo.csv>"
        ) from e


def normalizar_url(url: str) -> str:
    """
    Garante que a URL tenha esquema https:// e remove trailing slash.

    Exemplos:
        'empresa.bet.br'          → 'https://empresa.bet.br'
        'http://empresa.bet.br/'  → 'http://empresa.bet.br'
    """
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def carregar_dataframe(caminho_csv: str | None, url_csv: str) -> pd.DataFrame:
    """
    Carrega o CSV (local ou remoto) e retorna um DataFrame normalizado.

    Colunas esperadas no CSV do gov.br (adapte os nomes se necessário):
        - Razão Social / razao_social
        - CNPJ
        - Marca / nome_fantasia
        - Domínio / url / site
    """
    # O CSV oficial do gov.br tem:
    #  - separador ';'
    #  - primeira linha é um título descritivo (skip)
    #  - segunda linha é o cabeçalho real
    #  - colunas: [idx, PORTARIA, DENOMINACAO SOCIAL, CNPJ, MARCAS, DOMINIOS, REQUERIMENTO, ...]
    #  - uma empresa pode ter várias marcas/domínios em linhas subsequentes
    #    com células em branco nas colunas de razão social/CNPJ (precisa forward-fill)
    read_opts = dict(sep=";", skiprows=1, engine="python", dtype=str)

    if caminho_csv and Path(caminho_csv).exists():
        logger.info(f"Usando CSV local: {caminho_csv}")
        try:
            df = pd.read_csv(caminho_csv, encoding="utf-8-sig", **read_opts)
        except UnicodeDecodeError:
            df = pd.read_csv(caminho_csv, encoding="latin-1", **read_opts)
    else:
        conteudo = baixar_csv(url_csv)
        try:
            df = pd.read_csv(StringIO(conteudo), **read_opts)
        except Exception as e:
            raise RuntimeError(f"Erro ao parsear CSV: {e}") from e

    logger.info(f"CSV carregado — {len(df)} linhas, colunas: {list(df.columns)}")

    # Normaliza nomes de colunas para snake_case minúsculo sem acentos
    import unicodedata
    def _slug(s: str) -> str:
        s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
        s = re.sub(r"\s+", "_", s.strip().lower())
        return re.sub(r"[^\w]", "_", s)

    df.columns = [_slug(c) for c in df.columns]
    logger.debug(f"Colunas normalizadas: {list(df.columns)}")

    # Mapa de colunas conhecidas do CSV oficial para nomes padronizados
    mapa_colunas = {
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
    df = df.rename(columns={c: mapa_colunas[c] for c in df.columns if c in mapa_colunas})

    for col in ("razao_social", "cnpj", "marca", "url"):
        if col not in df.columns:
            df[col] = ""

    # Remove linhas totalmente vazias (rabicho de células ; ; ; no fim do CSV)
    df = df.dropna(how="all").reset_index(drop=True)

    # Forward-fill razao_social e cnpj: empresas com múltiplas marcas ocupam
    # várias linhas com essas colunas em branco após a primeira
    df["razao_social"] = df["razao_social"].replace(r"^\s*$", pd.NA, regex=True).ffill()
    df["cnpj"] = df["cnpj"].replace(r"^\s*$", pd.NA, regex=True).ffill()

    df["marca"] = df["marca"].fillna("").str.strip()
    df["url"] = df["url"].fillna("").str.strip().apply(normalizar_url)
    df["razao_social"] = df["razao_social"].fillna("").str.strip()
    df["cnpj"] = df["cnpj"].fillna("").str.strip()

    validas_antes = len(df)
    df = df[df["url"].str.startswith("http")].reset_index(drop=True)
    logger.info(
        f"{len(df)} domínios com URL válida "
        f"(removidas {validas_antes - len(df)} linhas sem URL)."
    )

    # Deduplica por URL (pode haver repetições entre arquivos)
    df = df.drop_duplicates(subset="url").reset_index(drop=True)
    logger.info(f"Após deduplicação por URL: {len(df)} registros.")
    return df[["razao_social", "cnpj", "marca", "url"]]


# ---------------------------------------------------------------------------
# Etapa 2 — Web scraping de emails
# ---------------------------------------------------------------------------

_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def verificar_robots(url_base: str, caminho: str = "/") -> bool:
    """
    Verifica se robots.txt permite acesso ao caminho indicado.

    Usa `requests` com UA de browser real para baixar o robots.txt
    (muitos sites .bet.br atrás de Cloudflare retornam 403 para o UA
    padrão do urllib, o que faria a lib bloquear tudo por engano).

    Política: na dúvida (erro de rede, 403/5xx, arquivo ausente),
    assume permitido — um bloqueio real vem apenas de um robots.txt
    legível que explicitamente proíbe o caminho.
    """
    parsed = urlparse(url_base)
    origem = f"{parsed.scheme}://{parsed.netloc}"

    with _ROBOTS_LOCK:
        em_cache = origem in _ROBOTS_CACHE
    if not em_cache:
        robots_url = f"{origem}/robots.txt"
        try:
            resp = requests.get(robots_url, headers=_headers_aleatorios(), timeout=TIMEOUT, verify=False)
            if resp.status_code == 200 and "text/html" not in resp.headers.get("content-type", ""):
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(resp.text.splitlines())
                with _ROBOTS_LOCK:
                    _ROBOTS_CACHE[origem] = rp
            else:
                with _ROBOTS_LOCK:
                    _ROBOTS_CACHE[origem] = None
        except Exception:
            with _ROBOTS_LOCK:
                _ROBOTS_CACHE[origem] = None

    with _ROBOTS_LOCK:
        rp = _ROBOTS_CACHE.get(origem)
    if rp is None:
        return True
    try:
        permitido = rp.can_fetch(HEADERS["User-Agent"], url_base + caminho)
        if not permitido:
            logger.debug(f"robots.txt bloqueia {url_base + caminho}")
        return permitido
    except Exception:
        return True


def buscar_html(url: str, sessao: requests.Session, tentativas: int = 3) -> str | None:
    """
    Faz GET na URL e retorna o HTML como string.

    Usa curl_cffi com impersonate='chrome' quando disponível.
    Fallback: requests padrão. Ambos aplicam backoff exponencial em 429.
    """
    if CFFI_DISPONIVEL:
        for t in range(tentativas):
            try:
                resp = cffi_requests.get(
                    url, impersonate="chrome",
                    timeout=TIMEOUT, allow_redirects=True, verify=False,
                )
                if resp.status_code == 429:
                    espera = 2 ** t + random.uniform(0, 2)
                    logger.debug(f"429 em {url} — aguardando {espera:.1f}s")
                    time.sleep(espera)
                    continue
                if resp.status_code < 400 and resp.text:
                    return resp.text
                logger.debug(f"curl_cffi HTTP {resp.status_code} em {url}")
                break
            except Exception as e:
                logger.debug(f"curl_cffi falhou em {url}: {e}")
                break

    # Fallback: requests padrão com backoff em 429
    for t in range(tentativas):
        try:
            resp = sessao.get(
                url, headers=_headers_aleatorios(), timeout=TIMEOUT,
                allow_redirects=True, verify=False,
            )
            if resp.status_code == 429:
                espera = 2 ** t + random.uniform(0, 2)
                logger.debug(f"429 em {url} — aguardando {espera:.1f}s")
                time.sleep(espera)
                continue
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.HTTPError:
            break
        except Exception as e:
            logger.debug(f"requests falhou em {url}: {e}")
            break

    # Última tentativa: algumas bets têm Cloudflare só no HTTPS
    if url.startswith("https://"):
        url_http = "http://" + url[8:]
        try:
            resp = sessao.get(
                url_http, headers=_headers_aleatorios(),
                timeout=TIMEOUT, allow_redirects=True, verify=False,
            )
            if resp.status_code < 400 and resp.text:
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
        except Exception:
            pass
    return None


# Navegador Playwright — instância POR THREAD.
# O sync_playwright usa greenlets vinculados à thread que o iniciou;
# compartilhar entre workers do ThreadPoolExecutor dispara greenlet.error.
# Solução: cada thread tem seu próprio (pw, browser) em threading.local.
_PLAYWRIGHT_LOCAL = threading.local()
_PLAYWRIGHT_ALL_CTXS: list = []  # registro global para fechar tudo ao final
_PLAYWRIGHT_ALL_LOCK = threading.Lock()

# Semáforo global — máximo 2 instâncias Chromium simultâneas.
# Independente de quantos workers estão no pool, nunca mais de 2 browsers
# rodarão ao mesmo tempo, evitando esgotamento de memória.
_PLAYWRIGHT_SEMAPHORE = threading.Semaphore(2)


def obter_browser_playwright():
    """Inicializa um Playwright por thread (thread-local) e reutiliza."""
    if not PLAYWRIGHT_DISPONIVEL:
        return None
    browser = getattr(_PLAYWRIGHT_LOCAL, "browser", None)
    if browser is not None:
        return browser
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        _PLAYWRIGHT_LOCAL.pw = pw
        _PLAYWRIGHT_LOCAL.browser = browser
        with _PLAYWRIGHT_ALL_LOCK:
            _PLAYWRIGHT_ALL_CTXS.append((pw, browser))
        logger.debug(
            f"Playwright iniciado (thread={threading.current_thread().name})"
        )
        return browser
    except Exception as e:
        logger.warning(f"Playwright não disponível nesta thread: {e}")
        _PLAYWRIGHT_LOCAL.browser = None
        return None


def fechar_browser_playwright():
    """Encerra TODOS os browsers Playwright criados (um por thread)."""
    with _PLAYWRIGHT_ALL_LOCK:
        ctxs = list(_PLAYWRIGHT_ALL_CTXS)
        _PLAYWRIGHT_ALL_CTXS.clear()
    for pw, browser in ctxs:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def _fechar_playwright_local() -> None:
    """Fecha o browser Playwright da thread atual (thread-local cleanup)."""
    browser = getattr(_PLAYWRIGHT_LOCAL, "browser", None)
    pw = getattr(_PLAYWRIGHT_LOCAL, "pw", None)
    if browser:
        try:
            browser.close()
        except Exception:
            pass
        _PLAYWRIGHT_LOCAL.browser = None
    if pw:
        try:
            pw.stop()
        except Exception:
            pass
        _PLAYWRIGHT_LOCAL.pw = None


import atexit
atexit.register(_fechar_playwright_local)



def buscar_html_com_js(url: str) -> str | None:
    """
    Renderiza a página via Playwright (Chromium headless) e retorna o HTML
    pós-execução de JavaScript. Usado como fallback quando o HTML estático
    não contém emails (SPAs React/Next/Vue).

    O semáforo _PLAYWRIGHT_SEMAPHORE garante no máximo 2 instâncias Chromium
    simultâneas, independente do tamanho do pool de threads.
    """
    with _PLAYWRIGHT_SEMAPHORE:
        browser = obter_browser_playwright()
        if browser is None:
            return None
        ctx = None
        page = None
        try:
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="pt-BR",
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            # Aguarda um pouco para o JS renderizar o rodapé
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            # Scroll progressivo — força renderização de componentes lazy/infinite scroll
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(800)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
            except Exception:
                pass
            html = page.content()
            return html
        except Exception as e:
            logger.debug(f"Playwright falhou em {url}: {e}")
            return None
        finally:
            try:
                if page:
                    page.close()
                if ctx:
                    ctx.close()
            except Exception:
                pass


def extrair_emails_do_html(html: str, url_origem: str) -> list[str]:
    """
    Extrai emails válidos do HTML de uma página.

    Prioriza emails encontrados no rodapé e em links mailto:,
    depois complementa com o restante do texto.
    Filtra endereços inválidos ou de sistema (noreply, bounce, etc.).
    """
    if not _BS4_OK:
        # Fallback sem bs4: extrai emails via regex diretamente do HTML
        import re as _re
        padrao = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
        encontrados = list(dict.fromkeys(_re.findall(padrao, html or "")))
        return encontrados[:20]
    soup = BeautifulSoup(html, "html.parser")
    emails_rodape: set[str] = set()
    emails_geral: set[str] = set()

    # JSON-LD / Schema.org — emails estruturados explícitos (alta confiança)
    jsonld_emails: set[str] = set()
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            obj = json.loads(script.string or "")
            _extrair_emails_jsonld(obj, jsonld_emails)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    emails_rodape.update(e.lower() for e in jsonld_emails)

    # Meta tags — varre qualquer atributo de qualquer <meta> procurando emails
    for meta in soup.find_all("meta"):
        attrs = meta.attrs or {}
        for attr_value in attrs.values():
            if isinstance(attr_value, str) and "@" in attr_value and "." in attr_value:
                for e in REGEX_EMAIL.findall(attr_value):
                    emails_rodape.add(e.lower())

    # Rodapé e variantes — área mais confiável para email de contato
    for seletor in ("footer", '[id*="footer"]', '[class*="footer"]',
                    '[id*="rodape"]', '[class*="rodape"]'):
        rodape = soup.select_one(seletor)
        if rodape:
            for e in REGEX_EMAIL.findall(rodape.get_text(" ")):
                emails_rodape.add(e.lower())

    # Seções de ajuda/suporte/FAQ e tags <address> — também alta confiança
    seletores_ajuda = (
        "address",
        '[id*="ajuda"]', '[class*="ajuda"]',
        '[id*="help"]', '[class*="help"]',
        '[id*="support"]', '[class*="support"]',
        '[id*="suporte"]', '[class*="suporte"]',
        '[id*="faq"]', '[class*="faq"]',
        '[id*="central"]', '[class*="central"]',
    )
    for seletor in seletores_ajuda:
        for bloco in soup.select(seletor):
            texto_bloco = bloco.get_text(" ")
            for e in REGEX_EMAIL.findall(texto_bloco):
                emails_rodape.add(e.lower())
            # Ofuscados dentro da seção de ajuda — desofusca e valida com regex normal
            for bruto in REGEX_EMAIL_OFUSCADO.findall(texto_bloco):
                limpo = _desofuscar(bruto).replace(" ", "").lower()
                if REGEX_EMAIL.match(limpo):
                    emails_rodape.add(limpo)

    # Varredura completa do texto
    texto_completo = soup.get_text(" ")
    for e in REGEX_EMAIL.findall(texto_completo):
        emails_geral.add(e.lower())

    # Ofuscações no texto bruto (não em atributos): "email [at] site [dot] com"
    for bruto in REGEX_EMAIL_OFUSCADO.findall(texto_completo):
        limpo = _desofuscar(bruto).replace(" ", "").lower()
        if REGEX_EMAIL.match(limpo):
            emails_geral.add(limpo)

    # Links mailto: têm alta confiança — vão para o set de rodapé
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.startswith("mailto:"):
            email = href.replace("mailto:", "").split("?")[0].strip().lower()
            if REGEX_EMAIL.match(email):
                emails_rodape.add(email)

    def e_valido(email: str) -> bool:
        if "@" not in email:
            return False
        local, _, dominio_email = email.partition("@")
        tld = dominio_email.rsplit(".", 1)[-1] if "." in dominio_email else ""
        return (
            not any(email.startswith(p) for p in EMAILS_IGNORADOS)
            and "example." not in dominio_email
            and 5 <= len(email) <= 100
            and len(local) >= 2
            and len(dominio_email) >= 5
            and "." in dominio_email
            and len(tld) >= 2
            and tld.isalpha()
            and tld.lower() not in ("png", "jpg", "jpeg", "gif", "svg", "webp",
                                     "css", "js", "html", "htm", "pdf",
                                     "mp4", "mp3", "woff", "ttf")
        )

    # Rodapé/mailto primeiro, depois restante do HTML
    resultado = [e for e in emails_rodape if e_valido(e)]
    resultado += [e for e in emails_geral - emails_rodape if e_valido(e)]

    # Deduplica preservando ordem
    vistos: set[str] = set()
    unicos = []
    for e in resultado:
        if e not in vistos:
            vistos.add(e)
            unicos.append(e)

    # Prioriza emails comerciais (contato@, sac@, suporte@, etc.) sem descartar
    # os demais — apenas reordena. Se nenhum for preferencial, mantém ordem.
    preferidos = [e for e in unicos if _e_preferido(e)]
    resto = [e for e in unicos if e not in preferidos]
    unicos = preferidos + resto

    if unicos:
        logger.debug(f"Emails encontrados em {url_origem}: {unicos}")
    return unicos


def coletar_email_empresa(url_base: str, sessao: requests.Session) -> tuple[str, str]:
    """
    Coleta o melhor email de contato encontrado para uma empresa.

    Estratégia:
      1. HTML estático (curl_cffi → requests) para home + subpáginas de contato
         — Se a home já retornar um email com prefixo preferencial
           (contato@, sac@, suporte@, ...), retorna imediatamente.
         — Caso contrário, acumula candidatos em todas as subpáginas
           e retorna o melhor ao final (preferenciais primeiro).
      2. Playwright (JS rendering) como fallback para SPAs — mesma lógica.

    Retorna o melhor email válido encontrado e o status da coleta.
    """
    if not url_base:
        return "", "sem_url"
    if not verificar_robots(url_base, "/"):
        return "", "bloqueado_robots"

    acessou_ao_menos_uma = False
    candidatos: list[str] = []  # ordem de chegada preservada; depois reordena

    # ── 1. Baixa a home ─────────────────────────────────────────────────
    html_home = buscar_html(url_base, sessao)
    links_dinamicos: list[str] = []
    if html_home is not None:
        acessou_ao_menos_uma = True
        emails = extrair_emails_do_html(html_home, url_base)
        if emails:
            # Early-exit: home já tem email preferencial
            if _e_preferido(emails[0]):
                logger.debug(f"[{url_base}] early-exit home preferencial: {emails[0]}")
                return emails[0], "encontrado"
            for e in emails:
                if e not in candidatos:
                    candidatos.append(e)
        links_dinamicos = _descobrir_links_uteis(html_home, url_base)
        logger.debug(
            f"[{url_base}] {len(links_dinamicos)} link(s) útil(eis) descoberto(s) na home"
        )

    # ── 2. Monta lista: dinâmicos primeiro, sitemap depois, subpáginas fixas ─
    paginas_visitadas: set[str] = {url_base.rstrip("/")}
    paginas: list[str] = []
    for link in links_dinamicos:
        norm = link.rstrip("/")
        if norm not in paginas_visitadas:
            paginas.append(link)
            paginas_visitadas.add(norm)

    # Sitemap.xml — muitas bets listam TODAS as páginas aqui
    try:
        links_sitemap = _descobrir_via_sitemap(url_base, sessao)
    except Exception as e:
        logger.debug(f"[{url_base}] sitemap falhou: {e}")
        links_sitemap = []
    for link in links_sitemap:
        norm = link.rstrip("/")
        if norm not in paginas_visitadas:
            paginas.append(link)
            paginas_visitadas.add(norm)

    for sub in SUBPAGINAS:
        candidato = (url_base + sub).rstrip("/")
        if candidato not in paginas_visitadas:
            paginas.append(url_base + sub)
            paginas_visitadas.add(candidato)

    # Limite total para não explodir em helpcenters com centenas de links
    paginas = paginas[:20]

    # Regex para detectar páginas de ajuda/help/faq para o deep-crawl nível 1
    _re_helpcenter = re.compile(r"ajuda|help|faq|central|suporte|support", re.IGNORECASE)

    # Fila dinâmica — permite anexar descobertas de helpcenters (profundidade 1)
    fila = list(paginas)
    idx = 0
    while idx < len(fila):
        url = fila[idx]
        idx += 1
        caminho = urlparse(url).path or "/"
        if not verificar_robots(url_base, caminho):
            continue
        logger.debug(f"[{url_base}] visitando {url}")
        html = buscar_html(url, sessao)
        if html is None:
            continue
        acessou_ao_menos_uma = True
        emails = extrair_emails_do_html(html, url)
        if emails:
            for e in emails:
                if e not in candidatos:
                    candidatos.append(e)
            # Early-exit se achou preferencial em qualquer subpágina
            if _e_preferido(emails[0]):
                logger.debug(f"[{url_base}] preferencial achado em {url}: {emails[0]}")
                break
        else:
            # Deep crawl: se é página de ajuda/help e não achou email,
            # descobre até 3 sub-links adicionais (profundidade máxima = 1).
            if _re_helpcenter.search(url) and len(fila) < 25:
                try:
                    sub_links = _descobrir_links_uteis(html, url)
                except Exception:
                    sub_links = []
                adicionados = 0
                for sl in sub_links:
                    norm = sl.rstrip("/")
                    if norm in paginas_visitadas:
                        continue
                    paginas_visitadas.add(norm)
                    fila.append(sl)
                    adicionados += 1
                    if adicionados >= 3:
                        break
        time.sleep(random.uniform(0.3, 0.8))

    # Se HTML estático já acumulou algum candidato, escolhe o melhor
    if candidatos:
        preferidos = [e for e in candidatos if _e_preferido(e)]
        melhor = preferidos[0] if preferidos else candidatos[0]
        logger.debug(f"[{url_base}] status=encontrado email={melhor}")
        return melhor, "encontrado"

    # ── 3. Fallback Playwright — foco em ajuda/help/suporte/contato ─────
    if PLAYWRIGHT_DISPONIVEL:
        alvos_js: list[str] = []
        vistos_js: set[str] = set()

        # Links dinâmicos que batem em palavras-chave relevantes
        for link in links_dinamicos:
            alvo = _normalizar(link)
            if any(p in alvo for p in ("ajuda", "help", "suporte", "support",
                                        "contato", "contact")):
                n = link.rstrip("/")
                if n not in vistos_js:
                    alvos_js.append(link)
                    vistos_js.add(n)

        # Fallbacks fixos — home + caminhos mais comuns
        fallbacks_js = [
            url_base,
            url_base + "/ajuda",
            url_base + "/help",
            url_base + "/central-de-ajuda",
            url_base + "/help-center",
            url_base + "/suporte",
            url_base + "/contato",
            url_base + "/fale-conosco",
        ]
        for fb in fallbacks_js:
            n = fb.rstrip("/")
            if n not in vistos_js:
                alvos_js.append(fb)
                vistos_js.add(n)

        alvos_js = alvos_js[:12]  # teto para evitar blow-up de tempo

        for url in alvos_js:
            logger.debug(f"[{url_base}] Playwright em {url}")
            html = buscar_html_com_js(url)
            if not html:
                continue
            acessou_ao_menos_uma = True
            emails = extrair_emails_do_html(html, url)
            if emails:
                for e in emails:
                    if e not in candidatos:
                        candidatos.append(e)
                if _e_preferido(emails[0]):
                    break
            time.sleep(random.uniform(0.5, 1.0))

        if candidatos:
            preferidos = [e for e in candidatos if _e_preferido(e)]
            melhor = preferidos[0] if preferidos else candidatos[0]
            logger.debug(f"[{url_base}] status=encontrado_js email={melhor}")
            return melhor, "encontrado_js"

    if not acessou_ao_menos_uma:
        logger.debug(f"[{url_base}] status=erro_conexao")
        return "", "erro_conexao"
    logger.debug(f"[{url_base}] status=nao_encontrado")
    return "", "nao_encontrado"


# ---------------------------------------------------------------------------
# Etapa 3 — Orquestrador principal
# ---------------------------------------------------------------------------

def processar_lista(
    df: pd.DataFrame,
    checkpoint: dict,
    limite: int | None = None,
) -> list[dict]:
    """
    Itera sobre o DataFrame e coleta emails para cada empresa.

    Respeita o checkpoint para pular empresas já processadas.
    Salva checkpoint periodicamente (a cada 5 registros).
    """
    resultados = []
    sessao = requests.Session()
    # Suprime warnings de SSL para sites com certificado inválido
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    total = len(df) if limite is None else min(limite, len(df))
    logger.info(f"Iniciando coleta de {total} empresa(s)...")

    for i, row in enumerate(df.itertuples(), start=1):
        if limite and i > limite:
            break

        url = row.url if hasattr(row, "url") else ""
        cnpj = str(row.cnpj).strip() if hasattr(row, "cnpj") else str(i)
        marca = str(row.marca).strip() if hasattr(row, "marca") else ""
        razao_social = str(row.razao_social).strip() if hasattr(row, "razao_social") else ""

        # Chave de checkpoint baseada na URL (única por domínio)
        chave = url or (cnpj if cnpj and cnpj != "nan" else str(i))

        if chave in checkpoint:
            logger.info(f"[{i}/{total}] {marca} — pulando (já processado).")
            resultados.append(checkpoint[chave])
            continue

        logger.info(f"[{i}/{total}] {marca} ({url})")

        email, status = coletar_email_empresa(url, sessao)

        registro = {
            "marca": marca,
            "razao_social": razao_social,
            "cnpj": cnpj,
            "url": url,
            "email_contato": email,
            "status": status,
            "data_coleta": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        resultados.append(registro)
        checkpoint[chave] = registro

        # Persiste checkpoint a cada 5 registros novos
        if i % 5 == 0:
            salvar_checkpoint(checkpoint)

        # Delay entre empresas diferentes
        if i < total:
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            logger.debug(f"Aguardando {delay:.1f}s antes da próxima requisição...")
            time.sleep(delay)

    salvar_checkpoint(checkpoint)
    _fechar_playwright_local()   # cleanup da thread atual
    fechar_browser_playwright()  # cleanup global (demais threads do pool)
    return resultados


# ---------------------------------------------------------------------------
# Exportação de resultados
# ---------------------------------------------------------------------------

def exportar_csv(resultados: list[dict], caminho: str = ARQUIVO_SAIDA) -> None:
    """Salva os resultados em CSV com as colunas padronizadas."""
    df_saida = pd.DataFrame(resultados, columns=[
        "marca", "razao_social", "cnpj", "url",
        "email_contato", "status", "data_coleta",
    ])
    df_saida.to_csv(caminho, index=False, encoding="utf-8-sig")
    logger.info(f"CSV de saída salvo em: {caminho}")


def gerar_relatorio(resultados: list[dict], caminho: str = ARQUIVO_RELATORIO) -> None:
    """Gera relatório .txt com estatísticas da coleta."""
    status_sucesso = {"encontrado", "encontrado_js"}
    total = len(resultados)
    encontrados = sum(1 for r in resultados if r["status"] in status_sucesso)
    encontrados_html = sum(1 for r in resultados if r["status"] == "encontrado")
    encontrados_js = sum(1 for r in resultados if r["status"] == "encontrado_js")
    nao_encontrados = sum(1 for r in resultados if r["status"] == "nao_encontrado")
    erros = total - encontrados - nao_encontrados

    dominios_erro = [
        r["url"] for r in resultados
        if r["status"] not in (*status_sucesso, "nao_encontrado", "sem_url")
    ]

    linhas = [
        "=" * 60,
        "  RELATÓRIO DE COLETA — BETS LEGALIZADAS BRASIL",
        f"  Gerado em: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
        f"Total de empresas processadas : {total}",
        f"Com email encontrado          : {encontrados} ({encontrados/total*100:.1f}%)" if total else "0",
        f"  └─ via HTML estático         : {encontrados_html}",
        f"  └─ via JS (Playwright)       : {encontrados_js}",
        f"Sem email (página rastreada)  : {nao_encontrados}",
        f"Com erro / inacessíveis       : {erros}",
        "",
    ]

    if dominios_erro:
        linhas += ["Domínios com erro:", ""]
        for d in sorted(dominios_erro):
            linhas.append(f"  - {d}")
        linhas.append("")

    bloqueados_robots = [r["url"] for r in resultados if r["status"] == "bloqueado_robots"]
    if bloqueados_robots:
        linhas += ["Bloqueados por robots.txt:", ""]
        for d in sorted(bloqueados_robots):
            linhas.append(f"  - {d}")
        linhas.append("")

    linhas += [
        "=" * 60,
        "Arquivo de dados: " + ARQUIVO_SAIDA,
        "Log completo   : " + ARQUIVO_LOG,
        "=" * 60,
    ]

    texto = "\n".join(linhas)
    Path(caminho).write_text(texto, encoding="utf-8")
    logger.info(f"Relatório salvo em: {caminho}")
    # Imprime com fallback seguro para terminais Windows (cp1252)
    try:
        print("\n" + texto)
    except UnicodeEncodeError:
        print("\n" + texto.encode("ascii", errors="replace").decode("ascii"))


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Processa argumentos de linha de comando."""
    parser = argparse.ArgumentParser(
        description="Coleta emails de bets legalizadas no Brasil.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--csv",
        metavar="ARQUIVO",
        help="Caminho para CSV local (se omitido, baixa do gov.br automaticamente).",
    )
    parser.add_argument(
        "--limite",
        type=int,
        metavar="N",
        help="Processa apenas as primeiras N empresas (útil para testes).",
    )
    parser.add_argument(
        "--reiniciar",
        action="store_true",
        help="Ignora checkpoint e reprocessa tudo do zero.",
    )
    parser.add_argument(
        "--saida",
        default=ARQUIVO_SAIDA,
        metavar="ARQUIVO",
        help=f"Nome do CSV de saída (padrão: {ARQUIVO_SAIDA}).",
    )
    return parser.parse_args()


def main() -> None:
    """Ponto de entrada principal do script."""
    args = parse_args()

    logger.info("=" * 60)
    logger.info("  COLETOR DE EMAILS — BETS LEGALIZADAS BR")
    logger.info("=" * 60)

    # Carrega ou zera checkpoint
    checkpoint = {} if args.reiniciar else carregar_checkpoint()
    if args.reiniciar:
        logger.info("Modo --reiniciar: checkpoint zerado.")

    # Etapa 1: carrega CSV
    df = carregar_dataframe(args.csv, URL_CSV_OFICIAL)

    # Etapa 2: coleta emails
    resultados = processar_lista(df, checkpoint, limite=args.limite)

    # Etapa 3: exporta resultados
    exportar_csv(resultados, args.saida)
    gerar_relatorio(resultados)

    logger.info("Coleta concluída.")


if __name__ == "__main__":
    main()
