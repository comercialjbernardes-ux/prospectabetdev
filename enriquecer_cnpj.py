"""
enriquecer_cnpj.py — Enriquecimento de dados via CNPJ
======================================================
Fontes (em ordem de prioridade):
  1. BrasilAPI  — https://brasilapi.com.br/api/cnpj/v1/{cnpj}  (gratuito, sem auth)
  2. ReceitaWS  — https://receitaws.com.br/v1/cnpj/{cnpj}      (gratuito, 3 req/min)
  3. Scraping   — rodapé do próprio site da bet

Campos retornados: localização completa + dados tributários.

Contexto regulatório (Lei 14.790/2023):
  - Bets recolhem 12% sobre GGR (Gross Gaming Revenue) à União
  - Operadores de grande porte (porte DEMAIS) tipicamente adotam Lucro Real
  - Simples Nacional é inviável para receita bruta > R$4,8M/ano
  - Lucro Presumido é mais comum em operadores de médio porte
"""

import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger("coletar_bets")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

# Padrões textuais de regime tributário encontrados em rodapés de sites
_PADROES_REGIME = [
    ("Simples Nacional", r"simples\s+nacional"),
    ("Lucro Real",       r"lucro\s+real"),
    ("Lucro Presumido",  r"lucro\s+presumido"),
    ("MEI",              r"\bmei\b"),
    ("Imune",            r"\bimune\b"),
    ("Isento",           r"\bisento\b"),
]

TIMEOUT = 15  # segundos

_CNPJ_CACHE_TTL_DIAS: int = 30  # Re-consulta a cada 30 dias


@dataclass
class _EntradaCache:
    dado: dict
    criado_em: float = field(default_factory=time.time)

    def expirado(self, ttl_dias: int = _CNPJ_CACHE_TTL_DIAS) -> bool:
        return (time.time() - self.criado_em) > (ttl_dias * 86400)


# Cache compartilhado por CNPJ limpo — evita consultas duplicadas quando
# múltiplas marcas pertencem ao mesmo CNPJ (ex: BPX tem 3 marcas).
# Valor None = placeholder "em processamento" por outro thread.
_CNPJ_CACHE: dict[str, Optional[_EntradaCache]] = {}
_CNPJ_LOCK = threading.Lock()


def _obter_do_cache(cnpj_limpo: str) -> Optional[dict]:
    """Retorna dado em cache se existir e não estiver expirado."""
    with _CNPJ_LOCK:
        entrada = _CNPJ_CACHE.get(cnpj_limpo)
        if entrada is None:
            return None
        if isinstance(entrada, _EntradaCache):
            if not entrada.expirado():
                return entrada.dado.copy()
            # Expirado — remove para forçar re-consulta
            del _CNPJ_CACHE[cnpj_limpo]
        return None


def _salvar_no_cache(cnpj_limpo: str, dado: dict) -> dict:
    """Salva dado no cache com timestamp atual."""
    with _CNPJ_LOCK:
        _CNPJ_CACHE[cnpj_limpo] = _EntradaCache(dado=dado)
    return dado.copy()

# Rate limiter dedicado para ReceitaWS (máx 3 req/min = 1 req a cada 20s).
# Um único lock garante serialização entre todos os workers, respeitando
# o rate limit da API sem banimento por IP.
_RECEITAWS_LOCK = threading.Lock()
_RECEITAWS_ULTIMO_REQUEST: float = 0.0
_RECEITAWS_MIN_INTERVALO: float = 21.0  # 21s com margem de segurança

# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------


def limpar_cnpj(cnpj: str) -> str:
    """Remove máscara do CNPJ: '55.238.676/0001-00' → '55238676000100'."""
    return re.sub(r"[^\d]", "", cnpj or "")


def _headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/html, */*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }


def _campos_vazios() -> dict:
    return {
        "logradouro": "", "numero": "", "complemento": "", "bairro": "",
        "municipio": "", "uf": "", "cep": "", "pais": "",
        "regime_tributario": "Não identificado",
        "porte_empresa": "",
        "situacao_cadastral": "",
        "capital_social": 0.0,
        "natureza_juridica": "",
        "data_abertura": "",
        "fonte_regime": "nao_identificado",
        "confiabilidade_dado": "baixa",
    }


# ---------------------------------------------------------------------------
# Inferência de regime tributário a partir dos dados da API
# ---------------------------------------------------------------------------


def inferir_regime(opcao_simples: bool, opcao_mei: bool,
                   capital: float, porte: str) -> tuple[str, bool]:
    """
    Infere regime tributário com base nos flags da Receita Federal.
    Retorna (regime, flag_confirmado_pela_receita).

    flag_confirmado_pela_receita=True significa que a Receita Federal confirmou
    explicitamente o regime (opcao_pelo_simples / opcao_pelo_mei).
    False significa que o regime foi inferido por heurística de porte/capital.

    Hierarquia:
      MEI > Simples Nacional > Lucro Real (por porte) > Lucro Presumido > Não identificado

    Nota: capital social ≠ receita bruta. A obrigatoriedade de Lucro Real
    pela Lei 9.718/98 art. 14 se baseia na receita bruta anual (> R$78M),
    não no capital social. Usamos o porte ("DEMAIS") como proxy mais confiável.
    """
    if opcao_mei:
        return "MEI", True
    if opcao_simples:
        return "Simples Nacional", True
    # Porte DEMAIS = empresa não enquadrada como ME/EPP → Lucro Real no setor de bets
    if str(porte).upper() == "DEMAIS":
        return "Lucro Real", False
    # ME/EPP sem opção pelo Simples → mais provável Lucro Presumido
    if str(porte).upper() in ("ME", "EPP", "MICRO EMPRESA"):
        return "Lucro Presumido", False
    return "Não identificado", False


# ---------------------------------------------------------------------------
# Fonte 1 — BrasilAPI
# ---------------------------------------------------------------------------


def consultar_brasilapi(cnpj: str) -> dict | None:
    """
    Consulta BrasilAPI para obter dados cadastrais do CNPJ.
    Tenta até 3 vezes com backoff exponencial em caso de 429.
    """
    cnpj_limpo = limpar_cnpj(cnpj)
    if len(cnpj_limpo) != 14:
        return None

    url = f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}"
    for tentativa in range(3):
        try:
            resp = requests.get(url, headers=_headers(), timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                espera = 2 ** tentativa + random.uniform(1, 3)
                logger.debug(f"BrasilAPI 429 — aguardando {espera:.1f}s (CNPJ {cnpj})")
                time.sleep(espera)
                continue
            if resp.status_code in (404, 400):
                logger.debug(f"BrasilAPI: CNPJ {cnpj} não encontrado")
                return None
            logger.debug(f"BrasilAPI HTTP {resp.status_code} para CNPJ {cnpj}")
            return None
        except Exception as e:
            logger.debug(f"BrasilAPI erro (tentativa {tentativa + 1}) para {cnpj}: {e}")
            if tentativa < 2:
                time.sleep(2 ** tentativa)
    return None


def _normalizar_brasilapi(dados: dict) -> dict:
    """Converte resposta da BrasilAPI no formato interno padronizado."""
    try:
        capital = float(dados.get("capital_social") or 0)
    except (ValueError, TypeError):
        capital = 0.0

    opcao_simples = bool(dados.get("opcao_pelo_simples"))
    opcao_mei = bool(dados.get("opcao_pelo_mei"))
    porte = str(dados.get("porte") or "")

    regime, confirmado = inferir_regime(opcao_simples, opcao_mei, capital, porte)

    return {
        "logradouro": dados.get("logradouro", ""),
        "numero": dados.get("numero", ""),
        "complemento": dados.get("complemento", ""),
        "bairro": dados.get("bairro", ""),
        "municipio": dados.get("municipio", ""),
        "uf": dados.get("uf", ""),
        "cep": str(dados.get("cep", "") or "").replace("-", ""),
        "pais": dados.get("descricao_pais", "BRASIL") or "BRASIL",
        "regime_tributario": regime,
        "porte_empresa": porte,
        "situacao_cadastral": dados.get("descricao_situacao_cadastral", ""),
        "capital_social": capital,
        "natureza_juridica": dados.get("natureza_juridica", ""),
        "data_abertura": dados.get("data_inicio_atividade", ""),
        "fonte_regime": "brasilapi" if confirmado else "inferido",
        "confiabilidade_dado": "alta" if confirmado else "media",
    }


# ---------------------------------------------------------------------------
# Fonte 2 — ReceitaWS (fallback)
# ---------------------------------------------------------------------------


def consultar_receitaws(cnpj: str) -> dict | None:
    """
    Consulta ReceitaWS — fallback quando BrasilAPI falha.
    Rate limit: ~3 req/min por IP → controlado globalmente por _RECEITAWS_LOCK.
    Todos os workers compartilham o lock, garantindo exatamente 1 req / 21s.
    """
    global _RECEITAWS_ULTIMO_REQUEST

    cnpj_limpo = limpar_cnpj(cnpj)
    if len(cnpj_limpo) != 14:
        return None

    url = f"https://receitaws.com.br/v1/cnpj/{cnpj_limpo}"
    for tentativa in range(2):
        try:
            with _RECEITAWS_LOCK:
                agora = time.time()
                espera = _RECEITAWS_MIN_INTERVALO - (agora - _RECEITAWS_ULTIMO_REQUEST)
                if espera > 0:
                    logger.debug(f"ReceitaWS rate limit: aguardando {espera:.1f}s (CNPJ {cnpj})")
                    time.sleep(espera)
                _RECEITAWS_ULTIMO_REQUEST = time.time()

            resp = requests.get(url, headers=_headers(), timeout=TIMEOUT)
            if resp.status_code == 200:
                dados = resp.json()
                if dados.get("status") == "ERROR":
                    logger.debug(f"ReceitaWS: CNPJ {cnpj} retornou erro: {dados.get('message')}")
                    return None
                return dados
            if resp.status_code == 429:
                logger.debug(f"ReceitaWS 429 — aguardando 30s extra (CNPJ {cnpj})")
                time.sleep(30)
                continue
        except Exception as e:
            logger.debug(f"ReceitaWS erro (tentativa {tentativa + 1}) para {cnpj}: {e}")
    return None


def _parse_capital_receitaws(valor: str | float | None) -> float:
    """
    Converte capital social do formato ReceitaWS para float.
    ReceitaWS retorna strings como 'R$ 100.000,00' ou números.
    """
    if valor is None:
        return 0.0
    try:
        if isinstance(valor, (int, float)):
            return float(valor)
        # Remove 'R$', espaços, pontos de milhar; troca vírgula por ponto
        limpo = re.sub(r"[R$\s]", "", str(valor))
        limpo = limpo.replace(".", "").replace(",", ".")
        return float(limpo)
    except (ValueError, TypeError):
        return 0.0


def _normalizar_receitaws(dados: dict) -> dict:
    """Converte resposta da ReceitaWS no formato interno padronizado."""
    capital = _parse_capital_receitaws(dados.get("capital_social"))
    porte = str(dados.get("porte") or "")

    # ReceitaWS aninha Simples Nacional em dados["simples"]["optante"]
    simples_info = dados.get("simples") or {}
    mei_info = dados.get("mei") or {}
    opcao_simples = bool(simples_info.get("optante")) if isinstance(simples_info, dict) else False
    opcao_mei = bool(mei_info.get("optante")) if isinstance(mei_info, dict) else False

    regime, confirmado = inferir_regime(opcao_simples, opcao_mei, capital, porte)

    return {
        "logradouro": dados.get("logradouro", ""),
        "numero": dados.get("numero", ""),
        "complemento": dados.get("complemento", ""),
        "bairro": dados.get("bairro", ""),
        "municipio": dados.get("municipio", ""),
        "uf": dados.get("uf", ""),
        "cep": str(dados.get("cep", "") or "").replace(".", "").replace("-", ""),
        "pais": "BRASIL",
        "regime_tributario": regime,
        "porte_empresa": porte,
        "situacao_cadastral": dados.get("situacao", ""),
        "capital_social": capital,
        "natureza_juridica": dados.get("natureza_juridica", ""),
        "data_abertura": dados.get("abertura", ""),
        # "receitaws" quando a Receita confirmou explicitamente; "inferido" por heurística
        "fonte_regime": "receitaws" if confirmado else "inferido",
        "confiabilidade_dado": "alta" if confirmado else "media",
    }


# ---------------------------------------------------------------------------
# Fonte 3 — Scraping do site da bet
# ---------------------------------------------------------------------------


def buscar_regime_no_site(html: str) -> str | None:
    """
    Busca padrões de regime tributário no HTML do site.
    Foca no rodapé onde dados fiscais costumam aparecer.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # bs4 opcional — fallback para busca por regex no HTML bruto
        texto = html or ""
        for regime, padrao in _PADROES_REGIME:
            if re.search(padrao, texto, re.IGNORECASE):
                return regime
        return None
    soup = BeautifulSoup(html, "html.parser")

    # Prioriza rodapé
    for seletor in ("footer", '[class*="footer"]', '[id*="footer"]',
                    '[class*="rodape"]', '[id*="rodape"]'):
        rodape = soup.select_one(seletor)
        if rodape:
            texto = rodape.get_text(" ")
            for regime, padrao in _PADROES_REGIME:
                if re.search(padrao, texto, re.IGNORECASE):
                    return regime

    # Busca no texto completo como fallback
    texto_completo = soup.get_text(" ")
    for regime, padrao in _PADROES_REGIME:
        if re.search(padrao, texto_completo, re.IGNORECASE):
            return regime

    return None


# ---------------------------------------------------------------------------
# Orquestrador principal
# ---------------------------------------------------------------------------


def enriquecer_empresa(cnpj: str, url: str = "",
                       sessao: requests.Session | None = None) -> dict:
    """
    Enriquece os dados de uma empresa com informações cadastrais e tributárias.

    Tenta as fontes em ordem: BrasilAPI → ReceitaWS → scraping do site.
    Usa cache por CNPJ limpo para evitar consultas duplicadas quando múltiplas
    marcas pertencem ao mesmo CNPJ.

    Retorna dict com campos de localização e tributação.
    """
    cnpj_limpo = limpar_cnpj(cnpj)
    if not cnpj_limpo or len(cnpj_limpo) != 14:
        logger.debug(f"CNPJ inválido: '{cnpj}'")
        return _campos_vazios()

    # 1. Verificar cache (com TTL de 30 dias)
    cached = _obter_do_cache(cnpj_limpo)
    if cached is not None:
        logger.debug(f"CNPJ {cnpj} — cache hit")
        return cached

    # Marca como "em processamento" — impede terceiro worker duplicar a consulta
    with _CNPJ_LOCK:
        # Double-check após adquirir o lock
        cached = _obter_do_cache(cnpj_limpo)
        if cached is not None:
            return cached
        _CNPJ_CACHE[cnpj_limpo] = None  # placeholder temporário

    # 2. BrasilAPI — fonte prioritária (sem rate limit severo)
    dados = consultar_brasilapi(cnpj)
    if dados:
        resultado = _normalizar_brasilapi(dados)
        logger.info(
            f"CNPJ {cnpj} — BrasilAPI OK | "
            f"regime: {resultado['regime_tributario']} | "
            f"UF: {resultado['uf']} | {resultado['municipio']}"
        )
        return _salvar_no_cache(cnpj_limpo, resultado)

    # 3. ReceitaWS — fallback com rate limit global controlado
    logger.debug(f"CNPJ {cnpj} — BrasilAPI falhou, tentando ReceitaWS...")
    dados = consultar_receitaws(cnpj)
    if dados:
        resultado = _normalizar_receitaws(dados)
        logger.info(
            f"CNPJ {cnpj} — ReceitaWS OK | "
            f"regime: {resultado['regime_tributario']} | "
            f"UF: {resultado['uf']}"
        )
        return _salvar_no_cache(cnpj_limpo, resultado)

    # 4. Scraping do site — último recurso
    if url and sessao:
        try:
            from coletar_bets import buscar_html
            html = buscar_html(url, sessao)
            if html:
                regime = buscar_regime_no_site(html)
                if regime:
                    resultado = _campos_vazios()
                    resultado.update({
                        "regime_tributario": regime,
                        "fonte_regime": "site",
                        "confiabilidade_dado": "media",
                    })
                    logger.info(f"CNPJ {cnpj} — regime '{regime}' encontrado no site")
                    return _salvar_no_cache(cnpj_limpo, resultado)
        except Exception as e:
            logger.debug(f"Scraping de regime falhou para {url}: {e}")

    # Garante que nunca retorna None — dict com campos vazios
    logger.info(f"CNPJ {cnpj} — regime não identificado em nenhuma fonte")
    return _salvar_no_cache(cnpj_limpo, _campos_vazios())
