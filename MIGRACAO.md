# MIGRACAO.md — Rastreamento de Mudanças vs `projeto bet/` Principal

Documento que registra **o que mudou neste fork** em relação ao sistema em produção, para guiar o merge-back ao final.

---

## Status das Etapas

| Etapa | Descrição | Status | Validado pelo usuário | Commit |
|---|---|---|---|---|
| **0** | Criar fork + GitHub repo + porta 5003 | ✅ Concluída | ✅ 2026-05-12 | `adf3817` |
| **1** | Quick-wins técnicos (`/health`, cache, circuit breaker, rate limit) | ✅ Concluída | ⏳ aguardando | `5d597e6` |
| **2** | Modularização (`data_manager.py`, `audit.py`, `export.py`, type hints) | ✅ Concluída | ⏳ aguardando | `446f45f` |
| **3** | Health Score composto + snapshots | ✅ Concluída | ⏳ aguardando | `045be8f` |
| **4** | Alertas inteligentes via webhook | ✅ Concluída | ⏳ aguardando | `5cdc95f` |
| **5** | AI Chat sobre os dados (Claude API) | ✅ Concluída | ⏳ aguardando | `20e6a97` |
| **6** | Anomalias + grupos empresariais | ✅ Concluída | ⏳ aguardando | `2ab36cc` |
| **7** | Email validation worker | ✅ Concluída | ⏳ aguardando | a commitar |

Legenda: ✅ Concluída · 🟡 Em andamento · ⚪ Pendente · ❌ Bloqueada

---

## Arquivos modificados neste fork

### Etapa 0 — Criação do fork

- `_start_server.py` — porta padrão alterada de `5002` para `5003`; docstring atualizada
- `README.md` — substituído pelo README do fork (DEV)
- `MIGRACAO.md` — **NOVO**, este arquivo

### Etapa 1 — Quick-wins técnicos ✅

**Arquivos novos:**
- `worker_utils.py` — classe `CircuitBreaker` reutilizável (backoff exponencial 5/10/20/60min após 3 falhas seguidas)

**`app.py`:**
- Imports top-level: `stats_snapshot`, `notificacoes` (eram lazy)
- Imports condicionais: `flask_limiter` para rate limiting
- Inicialização do `Limiter` após `app = Flask(__name__)`
- Cache TTL+mtime para health JSONs (`_health_cache` + `_ler_health_cached()`)
- Funções `_aplicar_url_health`, `_aplicar_afiliados_health`, `_aplicar_reclame_aqui_health` usam o cache
- `recarregar_dados()` chama `_invalidar_cache_health()` e atualiza `_ultima_recarga_ts`
- Nova rota **`/health`** com status agregado (ok/degraded/critical), info por worker, idade dos arquivos, estado dos circuit breakers
- `/api/editar` ganhou `@limiter.limit("10 per minute")` → retorna 429 após 10 reqs/min/IP
- `/api/snapshots`, `/api/notificacoes/*` usam módulos top-level (sem mais `import` dentro de função)

**`url_health.py`, `afiliados_health.py`, `reclame_aqui_health.py`:**
- Cada um cria `_circuit_breaker = CircuitBreaker(nome)`
- `_loop()` checa `deve_pausar()` antes de cada tick
- `_tick()` envolvido por `try/except` → `registrar_sucesso()` ou `registrar_falha()`
- Cada um expõe `estado_circuit_breaker()` consumido pelo `/health`
- Substituição de `print()` por `logger.info()`/`logger.error()`

**`requirements.txt`:**
- Nova dependência: `Flask-Limiter>=3.5.0`

**Validação:**
- ✅ `pytest tests/test_app.py` — 44/44 passing
- ✅ `curl /health` retorna JSON com 4 workers (todos `alive`)
- ✅ 11ª req em /api/editar/min retorna HTTP 429
- ✅ Cache de health: latência de `/api/dados` deve reduzir significativamente
- ✅ Principal (5002) continua rodando sem alteração

### Etapa 7 — Email Validation Worker ✅

**Arquivo novo:**
- `email_validation.py` (~200 linhas) — worker daemon validando sintaxe + MX record dos emails

**Validação em 2 níveis:**
1. **Sintaxe** — parser RFC do `email-validator`
2. **Deliverability** — consulta DNS para MX record do domínio

**Status possíveis:**
- `valid` — sintaxe OK + MX existe (badge verde)
- `no_mx` — sintaxe OK mas domínio não aceita email (badge amarelo)
- `invalid` — sintaxe inválida (badge vermelho)
- `erro` — falha de DNS (badge vermelho)
- `sem_email` — registro não tem email (oculto)

**`data_manager.py`:**
- Nova função `aplicar_email_validation(registros, ev_module)` aplica status em cada registro
- `recarregar()` ganha parâmetro `email_validation_module`

**`app.py`:**
- Import top-level
- Endpoint **`GET /api/email-validation`**
- Worker iniciado no boot block

**`requirements.txt`:**
- Nova dep: `email-validator>=2.0.0`

**Frontend:**
- Função JS `emailValidationDot(r)` retorna bolinha colorida ao lado do email
- 4 classes CSS: `.email-val-ok` (verde), `.email-val-warn` (amarelo), `.email-val-err` (vermelho), `.email-val-nd` (cinza)
- Polling a cada 5min em `/api/email-validation`
- Tooltip explicativo (sintaxe/MX status)

**Worker config:**
- TICK_SEGUNDOS=180 (3min)
- 5 emails/tick, 2 workers paralelos
- Re-check a cada 7 dias (deliverability raramente muda)
- Circuit breaker compartilhado com outros workers

**Resultados reais (4 emails cadastrados):**
- ✅ `afiliados@betvip.com` — válido
- ⚠️ **`contato@55w.bet.br`** — **domínio não aceita email** (sem MX) — descoberta importante!
- ✅ `suporte@betbra.bet.br` — válido
- ✅ `teste@teste.com.br` — válido

**Validação:**
- ✅ `pytest tests/` — **82/82 passing** (5 novos em `TestEmailValidation`)
- ✅ Mypy: Success no issues found
- ✅ Bolinhas amarelas visíveis na UI ao lado dos 3 emails problemáticos
- ✅ Worker rodando em background

---

### Etapa 6 — Anomalias + Grupos Empresariais ✅

**Arquivos novos:**
- `analise_grupos.py` (~140 linhas) — agrupa bets por CNPJ raiz (8 primeiros dígitos)
- `analise_anomalias.py` (~190 linhas) — 3 detectores: URLs caindo, queda RA, novas sem email

**`app.py`:**
- Import top-level dos 2 módulos
- Novo endpoint **`GET /api/holdings?top=N`** — retorna grupos com agregações (score médio, n_marcas, n_RA1000, etc.)
- Novo endpoint **`GET /api/anomalias`** — retorna 3 categorias detectadas

**Frontend:**
- Nova seção `<section class="insights-section">` entre KPIs e filtros
- 2 painéis lado a lado:
  - **🏢 Holdings** (2/3) — top 5 grupos com cards detalhados (razão social, CNPJ raiz, lista de marcas tag-style, score médio, flags como RA1000/inativas/emails)
  - **⚠️ Anomalias** (1/3) — 3 blocos coloridos (vermelho/laranja/azul) com top items
- Responsive: 1 coluna em telas < 1100px
- Carregamento assíncrono via fetch no DOMContentLoaded

**Achados nos dados reais (n=186):**
- **60 holdings empresariais detectadas** (com ≥2 marcas no mesmo CNPJ raiz)
- **A2FBR S.A.** = maior grupo (6 marcas: BETBRA, BETESPECIAL, BOLSA DE APOSTA, FULLTBET, MATCHBOOK, PINNACLE)
- **F12 DO BRASIL** = melhor grupo (3 marcas, 2 com RA1000, score médio 74)
- **25 URLs caindo recorrentemente** (3+ falhas em 24h)
- **15 oportunidades quentes** sem email cadastrado

**Validação:**
- ✅ `pytest tests/` — **77/77 passing** (9 novos: 4 em `TestAnaliseGrupos` + 5 em `TestAnaliseAnomalias`)
- ✅ Mypy: Success no issues found em 2 módulos novos
- ✅ Endpoints HTTP 200 com dados reais

---

### Etapa 5 — AI Chat sobre os Dados ✅

**Arquivo novo:**
- `ai_chat.py` (~340 linhas) — integração com Claude API, prompt caching, 3 tools, loop de tool use

**`app.py`:**
- Import top-level de `ai_chat` (com fallback gracioso se anthropic SDK ausente)
- Endpoint **`POST /api/chat`** — rate limit 20/min/IP, validação de tamanho

**`requirements.txt`:**
- Nova dep: `anthropic>=0.40.0`

**Frontend:**
- Botão flutuante laranja **"🤖 Pergunte"** (canto inferior direito)
- Drawer lateral com chat (transição suave, atalhos Cmd+K/Esc)
- Renderização Markdown leve no front (bullets, tabelas, negrito, código)
- Mostra metadata por resposta: modelo, tokens, cache hit, iterações
- Histórico de até 20 mensagens (10 turnos) preservado client-side

**Configuração:**
- Modelo via env var `AI_CHAT_MODEL` (default `claude-opus-4-7`)
- API key via env var `ANTHROPIC_API_KEY` (sem chave → endpoint retorna 503 elegante)

**Tools implementadas:**

| Tool | Função |
|---|---|
| `buscar_bets` | Filtra com 11 critérios (marca, uf, score_min, ra_ra1000, url_status, com_email, etc.) + ordenação |
| `obter_bet` | Detalhes completos de UMA bet por marca ou CNPJ |
| `estatisticas_gerais` | Stats agregadas (total, distribuição por score, top UFs, reputação RA) |

**Prompt caching:**
- System prompt (~1.5K tokens) cacheado com `cache_control: {"type": "ephemeral"}` (TTL 5min)
- Cache hit em 2ª+ query: ~90% de economia no input

**Custos estimados (Opus 4.7, 150 queries/dia):**
- Com cache hit: **~$0.015-0.025 por query** → **~$22-37/mês**
- Alternativa: setar `AI_CHAT_MODEL=claude-sonnet-4-6` reduz para **~$12-18/mês**

**Validação:**
- ✅ `pytest tests/` — **68/68 passing** (10 novos em `TestAiChat`)
- ✅ Mypy: Success no issues found em ai_chat.py
- ✅ `/api/chat` retorna 503 elegante sem API key
- ✅ Rate limit 20/min funcional
- ✅ Validação de tamanho de pergunta + histórico

---

### Etapa 4 — Alertas Inteligentes via Webhook ✅

**`notificacoes.py`:**
- Nova função genérica `notificar_evento(tipo, titulo, campos)` — substituível para qualquer worker
- Config default ganhou 3 novos tipos: `url_down`, `ra_score_drop`, `bet_removed`
- `notificar_edicao()` preservado (compat com `/api/editar`)

**`url_health.py` (4.1):**
- Histórico `_historico_falhas` por URL (lista de timestamps das últimas 24h, cap 10)
- `_detectar_alerta_url_down()` — se ≥3 falhas em 24h E sem alerta nas últimas 24h, dispara
- Cooldown de 24h por URL evita spam

**`reclame_aqui_health.py` (4.2):**
- `_detectar_alerta_ra_queda()` — compara nota atual com a anterior salva
- Dispara se queda ≥0.5 (limiar configurável `_LIMIAR_QUEDA_NOTA`)
- Inclui no payload: nota_anterior, nota_atual, queda, url_ra, reputacao

**`csv_sync.py` (4.3):**
- Quando uma bet vira `_removido_do_csv=True`, dispara alerta `bet_removed`
- Payload: marca, cnpj, url, removido_em

**Frontend (4.4):**
- Nova rota `/notificacoes` com página dedicada
- Form com checkboxes por tipo de alerta, URL do webhook, tipo (Slack/Discord/JSON)
- Botão "Disparar teste" para validar configuração antes de produção

**Validação:**
- ✅ `pytest tests/` — **58/58 passing** (7 novos em `TestAlertasInteligentes`)
- ✅ Rotas `/notificacoes`, `/api/notificacoes/config`, `/api/notificacoes/teste` HTTP 200
- ✅ 5 tipos de eventos suportados: edit, delete, url_down, ra_score_drop, bet_removed
- ✅ Cooldown de 24h em url_down (evita spam)

---

### Etapa 3 — Health Score Composto ✅

**Arquivos novos:**
- `health_score.py` (170 linhas) — função `calcular(reg)` retorna 0-100 composto de URL (25%) + nota RA (25%) + % resolvidas RA (20%) + afiliados (15%) + email (15%)

**`data_manager.py`:**
- `recarregar()` chama `health_score.aplicar_em_lote(dados)` após os merges
- Cada registro ganha `_health_score` (int 0-100) e `_health_score_classe` (excelente/bom/regular/ruim/critico)

**`app.py`:**
- `_aplicar_filtros_query()` aceita novo param `score_min` (filtro backend por score ≥ X)

**`stats_snapshot.py`:**
- `_calcular_stats()` agora inclui `score_medio`, `score_mediana`, `score_excelente`, `score_critico`
- Snapshot diário guarda evolução do score médio ao longo do tempo

**`url_health.py`:**
- Bugfix: `_listar_urls()` agora filtra chaves `_schema_version` etc. (overrides ganhou metadata na etapa 2)

**Frontend:**
- Nova coluna **"Score"** ordenável na tabela (entre Reclame Aqui e UF)
- Função JS `badgeScore(r)` com tooltip explicativo (URL/RA/afiliados/email)
- Novo filtro **"Score"** na barra: Excelente ≥80, Bom ≥65, Regular ≥50, Críticos <30
- 6 classes CSS de badge gradiente verde→vermelho
- Colspan empty-state: 14 → 15

**Distribuição real (n=186 bets):**
- ⭐ Excelente (≥80): 2  (F12.BET 85, H2 BET 83)
- 👍 Bom (≥65): 27
- ≈ Regular (≥50): 64
- 👎 Ruim (≥30): 78
- ⚠ Crítico (<30): 15

**Validação:**
- ✅ `pytest tests/` — **51/51 passing** (7 novos testes em `TestHealthScore`)
- ✅ Mypy: Success no issues found em 5 módulos
- ✅ Filtro `?score_min=80` reduz para 3 bets (correto)
- ✅ BETANO (URL bloqueado, sem email): 67 — penaliza falta de email + bot-block
- ✅ F12.BET (tudo ok + RA1000 9.7 + afil sim): 85

---

### Etapa 2 — Modularização ✅

**Arquivos novos:**
- `data_manager.py` (400 linhas) — estado global, overrides, carregamento, cache de health, merges (url/afiliados/RA), orquestração `recarregar()`
- `audit.py` (203 linhas) — append-only JSONL, rotação, leitura paginada com filtros
- `export.py` (100 linhas) — exportação CSV/XLSX com fallback gracioso

**`app.py` (1286 → 867 linhas, -32%):**
- Globals (`_dados`, `_overrides`, `_health_cache`, `_ultima_recarga_ts`) → `data_manager`
- Funções audit (`_rotacionar_audit_log`, `_registrar_auditoria`) → `audit`
- Função `/auditoria` enxuta: delega para `audit.ler_paginado()`
- Função `/api/exportar` enxuta: delega para `export.exportar()`
- Shims preservados para compat (`_display_afiliados`, `_aplicar_overrides`, etc.)

**Type hints + mypy:**
- `python -m mypy data_manager.py audit.py worker_utils.py export.py --ignore-missing-imports` → **Success: no issues found in 4 source files**
- `python -m mypy reclame_aqui_health.py --ignore-missing-imports` → **Success: no issues found in 1 source file**

**Validação:**
- ✅ `pytest tests/test_app.py` — 44/44 passing
- ✅ `/auditoria` HTTP 200 com listagem de eventos
- ✅ `/health` HTTP 200 com status dos workers
- ✅ Edição inline + audit log funcional
- ✅ Principal (5002) e fork (5003) rodando em paralelo

### ...

---

## Merge-back para o principal

Quando todas as etapas estiverem aprovadas:

1. **Backup do principal**:
   ```powershell
   $data = Get-Date -Format "yyyy-MM-dd"
   robocopy "C:\Users\Administrator\Documents\venda feita\projeto bet" "C:\Users\Administrator\Documents\venda feita\projeto bet_BACKUP_$data" /E
   ```

2. **Diff arquivo-a-arquivo** (excluindo `dados/` e `.git/`):
   ```powershell
   Compare-Object `
     (Get-ChildItem "C:\...\projeto bet" -Recurse -Exclude "dados","*.log","__pycache__") `
     (Get-ChildItem "C:\...\prospector-bets-dev" -Recurse -Exclude "dados","*.log","__pycache__","MIGRACAO.md") `
     -Property Name,Length
   ```

3. **Aplicar arquivos do fork sobre o principal** preservando `dados/` do principal (que tem dados frescos da produção)

4. **Restaurar porta 5002** em `_start_server.py`

5. **Restaurar README.md original** (não copiar o README do fork)

6. **Rodar `pytest tests/`** + smoke test em `http://127.0.0.1:5002/`

7. **Commit no `Venda-feita`** com mensagem detalhada das etapas mergeadas

8. **Manter o fork `prospector-bets-dev`** vivo como ambiente de homologação para próximas evoluções
