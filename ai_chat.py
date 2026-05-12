# mypy: disable-error-code="union-attr,attr-defined,arg-type"
"""
ai_chat.py — Endpoint de chat AI sobre os dados (Claude API + tool use + prompt caching)
=========================================================================================
Permite que a equipe faça perguntas em linguagem natural sobre as bets monitoradas,
sem precisar criar UI nova para cada filtro.

Exemplos de queries que funcionam:
    "Quais bets com nota RA acima de 8 em SP?"
    "Top 5 bets sem email"
    "Compare BETANO vs PIXBET"
    "Quantas bets críticas (score < 30) temos?"
    "Lista as bets com URL bloqueada"

Arquitetura:
- Modelo: Claude Opus 4.7 (default; configurável via env `AI_CHAT_MODEL`)
- Prompt caching no system prompt (TTL 5min) — economia ~90% em queries repetidas
- 3 tools: buscar_bets, obter_bet, estatisticas_gerais
- Loop de tool use (até 10 iterações) usando o padrão manual
- Sem streaming na v1 (max_tokens=4096, bem abaixo do limite de timeout)

Custo estimado com prompt caching ativo:
    Opus 4.7:  ~$0.015-0.025 por query  (~$22-37/mês com 150 queries/dia)
    Sonnet 4.6: ~$0.008-0.012 por query (~$12-18/mês com 150 queries/dia) — alternativa mais barata

API key: variável de ambiente `ANTHROPIC_API_KEY`.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

import data_manager
import health_score
from logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

# Modelo — pode ser sobrescrito via env (ex: "claude-sonnet-4-6" para reduzir custo)
MODEL: str = os.environ.get("AI_CHAT_MODEL", "claude-opus-4-7")
MAX_TOKENS: int = 4096
MAX_ITERACOES: int = 10

# Cliente lazy (só instancia quando há API key)
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Lazy init do client. Lança RuntimeError se ANTHROPIC_API_KEY não está setada."""
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY não configurada. "
                "Defina em variável de ambiente do sistema."
            )
        _client = anthropic.Anthropic()
    return _client


def disponivel() -> bool:
    """True se o módulo está pronto para uso (API key configurada)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# System prompt (cacheado — instruções estáveis sobre o domínio)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é o assistente do Prospector Bets, um sistema interno que monitora casas de apostas regulamentadas no Brasil (Lei 14.790/23, Ministério da Fazenda).

# Sua função
Ajudar a equipe a:
- Filtrar e analisar bets monitoradas (~186 bets atualmente)
- Identificar oportunidades de prospecção (bets sem email/contato)
- Detectar problemas (URLs caindo, queda de reputação no Reclame Aqui)
- Comparar bets por diversos critérios

# Schema dos dados (cada bet tem estes campos)
- `marca` — nome comercial (ex: BETANO, PIXBET, F12.BET)
- `cnpj` — CNPJ formatado
- `razao_social` — nome legal da empresa
- `url` — URL principal
- `email_contato` — email comercial (vazio se não encontrado)
- `uf`, `municipio` — localização
- `porte_empresa` — porte cadastral (ME / EPP / DEMAIS)
- `_url_health_status` — saúde da URL:
   - `ok` (200 OK)
   - `redirect` (30x)
   - `bloqueado` (4xx — site ATIVO mas bloqueia bots; **NÃO é inativo!**)
   - `erro_http` / `erro_conexao` / `erro_ssl` / `erro_dns` / `timeout` (offline)
   - `desconhecido` (ainda não checada)
- `_url_inativa` — bool, true se site está realmente offline (5xx/DNS/timeout)
- `_afiliados_display` — `sim` / `nao` / `nao_encontrado`
- `_ra_status` — `encontrado` / `nao_encontrado` / `desconhecido`
- `_ra_nota` — nota Reclame Aqui (0-10), só se `_ra_status=encontrado`
- `_ra_reputacao` — `RA1000` / `Ótimo` / `Bom` / `Regular` / `Ruim` / `Péssimo` / `Sem índice` / `Não recomendada`
- `_ra_ra1000` — bool, true se tem selo RA1000 (top do Reclame Aqui)
- `_ra_reclamacoes` — total de reclamações
- `_ra_resolvidas` — % de reclamações resolvidas
- `_health_score` — **score composto 0-100** combinando:
   - URL health (25%) + nota RA (25%) + % resolvidas RA (20%) + afiliados (15%) + email (15%)
- `_health_score_classe` — `excelente` (≥80) / `bom` (≥65) / `regular` (≥50) / `ruim` (≥30) / `critico` (<30)

# Como usar as ferramentas
- Use `buscar_bets` para listar bets com filtros (preferida quando o usuário quer ver várias)
- Use `obter_bet` quando o usuário pede detalhes de UMA bet específica (por marca ou CNPJ)
- Use `estatisticas_gerais` para perguntas agregadas ("quantas bets ao todo?", "distribuição por estado")

# Diretrizes de resposta
- Seja **conciso e direto** — esta é uma equipe interna, sem floreios.
- Use **markdown**: bullets para listas, tabelas para comparações.
- Sempre inclua o `_health_score` quando for relevante.
- Quando comparar 2+ bets, use tabela markdown com header.
- Se a pergunta for ambígua, escolha uma interpretação razoável e prossiga (não fique perguntando).
- Responda em português do Brasil.
"""


# ---------------------------------------------------------------------------
# Definições das tools
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "buscar_bets",
        "description": (
            "Filtra e retorna bets que atendem aos critérios. Retorna até 30 resultados "
            "(configurável até 50) com campos essenciais. Use `score_min` para focar em bets "
            "de alta qualidade. Use `ordenar_por` para controlar o ranking. "
            "Filtros são combinados com AND."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "marca":        {"type": "string", "description": "Filtro por nome de marca (case-insensitive, substring)"},
                "uf":           {"type": "string", "description": "Sigla do estado, 2 letras (ex: 'SP', 'RJ')"},
                "score_min":    {"type": "integer", "description": "Score de saúde mínimo (0-100). Ex: 80=excelentes, 65=bom+, 50=regular+"},
                "ra_status":    {"type": "string", "enum": ["encontrado", "nao_encontrado", "desconhecido"]},
                "ra_ra1000":    {"type": "boolean", "description": "True = apenas bets com selo RA1000"},
                "url_status":   {"type": "string", "enum": ["online", "ok", "redirect", "bloqueado", "erro", "desconhecido"],
                                 "description": "Saúde da URL. 'online' = ok+redirect+bloqueado (site acessível). 'erro' = qualquer status de erro."},
                "com_email":    {"type": "boolean", "description": "True = somente com email; False = somente sem email"},
                "com_afiliados":{"type": "boolean", "description": "True = somente com afiliados detectados"},
                "porte":        {"type": "string", "description": "Filtro exato por porte_empresa"},
                "ordenar_por":  {"type": "string", "enum": ["score", "ra_nota", "marca", "reclamacoes"],
                                 "description": "Critério de ordenação. score/ra_nota/reclamacoes = descendente; marca = alfabético."},
                "limite":       {"type": "integer", "description": "Número máximo de resultados (default 30, máx 50)"},
            },
        },
    },
    {
        "name": "obter_bet",
        "description": (
            "Retorna **todos** os campos de uma bet específica, identificada por marca (exata "
            "ou substring) ou CNPJ. Use quando o usuário pergunta sobre UMA bet específica."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identificador": {"type": "string", "description": "Nome da marca (ex: 'BETANO') ou CNPJ (com ou sem máscara)"},
            },
            "required": ["identificador"],
        },
    },
    {
        "name": "estatisticas_gerais",
        "description": (
            "Retorna estatísticas agregadas do sistema: total de bets, distribuição por "
            "classe de score, top 10 UFs, contagem online/offline, distribuição de reputação RA."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# Subset de campos retornados em buscar_bets (cabe no contexto sem inflar tokens)
_CAMPOS_RESUMO = (
    "marca", "cnpj", "uf", "municipio",
    "_health_score", "_health_score_classe",
    "_ra_nota", "_ra_reputacao", "_ra_ra1000", "_ra_reclamacoes",
    "_url_health_status", "_url_inativa",
    "email_contato", "_afiliados_display", "url",
)


def _resumir(reg: dict) -> dict:
    """Filtra um registro para os campos essenciais."""
    return {k: reg.get(k) for k in _CAMPOS_RESUMO}


# ---------------------------------------------------------------------------
# Handlers das tools
# ---------------------------------------------------------------------------

_ONLINE_STATUS = {"ok", "redirect", "bloqueado"}
_ERRO_STATUS = {"erro_http", "erro_conexao", "erro_ssl", "erro_dns", "timeout", "erro"}


def _tool_buscar_bets(args: dict) -> dict:
    dados = data_manager.dados_snapshot()
    marca         = (args.get("marca") or "").lower()
    uf            = (args.get("uf") or "").upper()
    score_min     = args.get("score_min")
    ra_status     = args.get("ra_status")
    ra_ra1000     = args.get("ra_ra1000")
    url_status    = args.get("url_status")
    com_email     = args.get("com_email")
    com_afiliados = args.get("com_afiliados")
    porte         = args.get("porte")
    ordenar_por   = args.get("ordenar_por") or "score"
    limite        = min(max(1, args.get("limite") or 30), 50)

    resultado: list[dict] = []
    for r in dados:
        if marca and marca not in (r.get("marca") or "").lower():
            continue
        if uf and (r.get("uf") or "").upper() != uf:
            continue
        if score_min is not None and int(r.get("_health_score") or 0) < score_min:
            continue
        if ra_status and r.get("_ra_status") != ra_status:
            continue
        if ra_ra1000 is True and not r.get("_ra_ra1000"):
            continue
        if ra_ra1000 is False and r.get("_ra_ra1000"):
            continue
        if url_status:
            st = r.get("_url_health_status") or "desconhecido"
            if url_status == "online" and st not in _ONLINE_STATUS:
                continue
            if url_status == "erro" and st not in _ERRO_STATUS:
                continue
            if url_status in ("ok", "redirect", "bloqueado", "desconhecido") and st != url_status:
                continue
        if com_email is True and not r.get("email_contato"):
            continue
        if com_email is False and r.get("email_contato"):
            continue
        if com_afiliados is True and r.get("_afiliados_display") != "sim":
            continue
        if porte and r.get("porte_empresa") != porte:
            continue
        resultado.append(r)

    if ordenar_por == "score":
        resultado.sort(key=lambda r: -(r.get("_health_score") or 0))
    elif ordenar_por == "ra_nota":
        resultado.sort(key=lambda r: -(r.get("_ra_nota") or 0))
    elif ordenar_por == "reclamacoes":
        resultado.sort(key=lambda r: -(r.get("_ra_reclamacoes") or 0))
    elif ordenar_por == "marca":
        resultado.sort(key=lambda r: (r.get("marca") or "").lower())

    return {
        "total_encontrado": len(resultado),
        "exibindo":         min(limite, len(resultado)),
        "bets":             [_resumir(r) for r in resultado[:limite]],
    }


def _tool_obter_bet(args: dict) -> dict:
    ident = (args.get("identificador") or "").strip()
    if not ident:
        return {"erro": "Identificador vazio"}
    so_digitos = "".join(c for c in ident if c.isdigit())
    ident_lower = ident.lower()
    dados = data_manager.dados_snapshot()
    # 1) Match exato por CNPJ
    if so_digitos and len(so_digitos) >= 8:
        for r in dados:
            cnpj_r = "".join(c for c in (r.get("cnpj") or "") if c.isdigit())
            if so_digitos in cnpj_r:
                return r
    # 2) Match exato por marca
    for r in dados:
        if (r.get("marca") or "").lower() == ident_lower:
            return r
    # 3) Match por substring de marca
    for r in dados:
        if ident_lower in (r.get("marca") or "").lower():
            return r
    return {"erro": f"Bet não encontrada: '{ident}'"}


def _tool_estatisticas_gerais(_args: dict) -> dict:
    dados = data_manager.dados_snapshot()
    total = len(dados)
    por_score = {"excelente": 0, "bom": 0, "regular": 0, "ruim": 0, "critico": 0}
    for r in dados:
        cls = r.get("_health_score_classe") or health_score.classificar(r.get("_health_score") or 0)
        if cls in por_score:
            por_score[cls] += 1
    por_uf: dict[str, int] = {}
    for r in dados:
        por_uf[r.get("uf") or "?"] = por_uf.get(r.get("uf") or "?", 0) + 1
    top_uf = dict(sorted(por_uf.items(), key=lambda x: -x[1])[:10])
    online  = sum(1 for r in dados if (r.get("_url_health_status") or "") in _ONLINE_STATUS)
    offline = sum(1 for r in dados if r.get("_url_inativa"))
    com_email = sum(1 for r in dados if r.get("email_contato"))
    com_afil  = sum(1 for r in dados if r.get("_afiliados_display") == "sim")
    por_ra: dict[str, int] = {}
    for r in dados:
        por_ra[r.get("_ra_reputacao") or "sem dado"] = por_ra.get(r.get("_ra_reputacao") or "sem dado", 0) + 1
    return {
        "total_bets":         total,
        "online":             online,
        "offline":            offline,
        "com_email":          com_email,
        "sem_email":          total - com_email,
        "com_afiliados":      com_afil,
        "distribuicao_score": por_score,
        "top_10_ufs":         top_uf,
        "reputacao_ra":       dict(sorted(por_ra.items(), key=lambda x: -x[1])),
    }


TOOL_HANDLERS = {
    "buscar_bets":         _tool_buscar_bets,
    "obter_bet":           _tool_obter_bet,
    "estatisticas_gerais": _tool_estatisticas_gerais,
}


# ---------------------------------------------------------------------------
# Loop de chat
# ---------------------------------------------------------------------------

def responder(pergunta: str, historico: list | None = None) -> dict:
    """
    Processa uma pergunta com loop de tool use.

    :param pergunta: texto do usuário
    :param historico: lista de mensagens anteriores (formato Anthropic),
                      ou None para nova conversa
    :returns: dict com chaves:
        resposta:        texto final do assistente (markdown)
        iteracoes:       número de iterações (passos de tool use + 1 final)
        tokens_input:    total acumulado
        tokens_output:   total acumulado
        cache_read:      tokens lidos do cache (economia ~90% vs input normal)
        cache_creation:  tokens escritos no cache (custa ~125% do input)
        modelo:          string do modelo usado
        tools_chamadas:  lista de tools invocadas (debug)
    """
    if not disponivel():
        return {
            "erro": "API key não configurada",
            "resposta": "❌ Chat indisponível: variável ANTHROPIC_API_KEY não está configurada no servidor.",
        }

    client = _get_client()
    messages = list(historico or [])
    messages.append({"role": "user", "content": pergunta})

    iteracoes = 0
    cache_read_total = 0
    cache_creation_total = 0
    input_total = 0
    output_total = 0
    tools_chamadas: list[str] = []

    while True:
        iteracoes += 1
        if iteracoes > MAX_ITERACOES:
            return {
                "resposta": f"⚠️ Loop muito longo (>{MAX_ITERACOES} iterações). Tente reformular a pergunta.",
                "erro": "max_iter",
            }

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # cacheia system prompt (5min TTL)
                }],
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.RateLimitError as e:
            logger.warning(f"Rate limited pela Anthropic: {e}")
            return {"erro": "rate_limit", "resposta": "⚠️ Limite de taxa da Anthropic atingido. Tente novamente em alguns segundos."}
        except anthropic.AuthenticationError:
            return {"erro": "auth", "resposta": "❌ API key inválida ou expirada."}
        except anthropic.APIError as e:
            logger.exception(f"Erro Anthropic API")
            return {"erro": "api_error", "resposta": f"❌ Erro na API: {e}"}

        usage = response.usage
        input_total += getattr(usage, "input_tokens", 0) or 0
        output_total += getattr(usage, "output_tokens", 0) or 0
        cache_read_total += getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation_total += getattr(usage, "cache_creation_input_tokens", 0) or 0

        # Sempre append o content completo (preserva tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extrai texto final
            texto = ""
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    texto += block.text
            return {
                "resposta":         texto.strip(),
                "iteracoes":        iteracoes,
                "tokens_input":     input_total,
                "tokens_output":    output_total,
                "cache_read":       cache_read_total,
                "cache_creation":   cache_creation_total,
                "modelo":           MODEL,
                "tools_chamadas":   tools_chamadas,
            }

        if response.stop_reason == "tool_use":
            # Executa cada tool_use block
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tools_chamadas.append(block.name)
                handler = TOOL_HANDLERS.get(block.name)
                if not handler:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Tool desconhecida: {block.name}",
                        "is_error": True,
                    })
                    continue
                try:
                    result = handler(dict(block.input))
                    content_str = json.dumps(result, ensure_ascii=False, default=str)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content_str,
                    })
                except Exception as e:
                    logger.exception(f"Erro executando tool {block.name}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Erro ao executar: {e}",
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Outro stop_reason (refusal, max_tokens, etc.)
        return {
            "resposta": f"⚠️ Resposta interrompida: {response.stop_reason}",
            "erro":     response.stop_reason,
        }
