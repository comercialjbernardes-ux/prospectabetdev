# Prospector Bets — Documentação Técnica Completa

> **Versão:** 2.0 — gerada em 2026-05-02  
> **Ambiente:** Windows 10/11 · Python 3.12 (portable) · Flask 3.x  
> **Servidor:** `python _start_server.py` → `http://127.0.0.1:5002`

---

## Índice

1. [Visão Geral & Propósito](#1-visão-geral--propósito)
2. [Arquitetura do Sistema](#2-arquitetura-do-sistema)
3. [Módulos — Referência Técnica](#3-módulos--referência-técnica)
   - 3.1 app.py
   - 3.2 pipeline.py
   - 3.3 csv_sync.py
   - 3.4 url_health.py
   - 3.5 afiliados_health.py
   - 3.6 coletar_bets.py
   - 3.7 coletar_afiliados.py
   - 3.8 enriquecer_cnpj.py
   - 3.9 enriquecer_base.py
   - 3.10 json_store.py
   - 3.11 logging_config.py
4. [Frontend — Design System](#4-frontend--design-system)
   - 4.1 templates/index.html
   - 4.2 static/style.css
   - 4.3 static/app.js
5. [APIs — Referência de Endpoints](#5-apis--referência-de-endpoints)
6. [Fluxo de Dados](#6-fluxo-de-dados)
7. [Estrutura de Arquivos](#7-estrutura-de-arquivos)
8. [Dados e Schemas](#8-dados-e-schemas)
9. [Workers em Background](#9-workers-em-background)
10. [Dívidas Técnicas e Melhorias](#10-dívidas-técnicas-e-melhorias)

---

## 1. Visão Geral & Propósito

O **Prospector Bets** é um dashboard analítico para monitoramento das casas de apostas regulamentadas pelo Ministério da Fazenda / Secretaria de Prêmios e Apostas (SPA) do Brasil.

### Objetivos principais
| Objetivo | Mecanismo |
|---|---|
| Manter lista atualizada das bets legalizadas | Sincronização periódica com CSV oficial do gov.br (6h) |
| Enriquecer cada empresa com dados CNPJ | BrasilAPI → ReceitaWS → scraping |
| Coletar emails de contato | Scraping estático + Playwright JS |
| Detectar programas de afiliados | Scraping de subpáginas + heurísticas de domínio |
| Monitorar saúde das URLs | Daemon de health-check contínuo |
| Expor dashboard analítico | Flask + Chart.js + tabela paginada |

### Stack tecnológica
```
Backend    : Python 3.12, Flask 3.x, Requests, curl_cffi, Playwright (opcional)
Frontend   : Vanilla JS (ES2020+), Chart.js 4.4, SheetJS/xlsx
Dados      : JSON files (atomic writes), CSV (pandas), JSONL (audit log)
Python env : C:\PythonPortable\python312\python.exe (embeddable, sem adicionar ao PATH)
```

### Restrições de ambiente conhecidas
- `bs4` (BeautifulSoup) **não está instalado** → todos os módulos que a usam têm fallbacks via regex
- `playwright` é opcional → scrapers degradam para requests estático
- `curl_cffi` é opcional → scrapers degradam para `requests` padrão
- Python no PATH do Windows é o stub do Microsoft Store (não funcional) — sempre usar o caminho absoluto

---

## 2. Arquitetura do Sistema

```
┌─────────────────────────────────────────────────────────────────┐
│                        FONTES EXTERNAS                          │
│  gov.br CSV  ──►  csv_sync.py      BrasilAPI / ReceitaWS        │
│  Sites das bets ─► coletar_bets.py  └──► enriquecer_cnpj.py    │
└───────────────────────────┬─────────────────────────────────────┘
                            │ dados/bets_enriquecidas.json
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      CAMADA DE DADOS                            │
│                                                                  │
│  bets_enriquecidas.json   overrides.json   audit_log.jsonl      │
│  url_health.json          afiliados_health.json                  │
│  csv_sync_status.json     checkpoint.json                        │
│                                                                  │
│  Escrita: json_store.py (atomic tmp→rename, backup, locks)      │
└───────────────────────┬─────────────────────────────────────────┘
                        │
            ┌───────────┼──────────────┐
            ▼           ▼              ▼
     url_health.py  afiliados_health.py  csv_sync.py
     (daemon 60s)   (daemon 60s)         (daemon 6h)
            │           │              │
            └───────────┼──────────────┘
                        │ merge em memória
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                       app.py (Flask)                            │
│                                                                  │
│  _dados: list[dict]  ── em memória, reidratado a cada request  │
│                                                                  │
│  recarregar_dados()                                              │
│    ├─ _carregar_dados()         ← JSON base                     │
│    ├─ _aplicar_overrides()      ← edições manuais               │
│    ├─ _aplicar_url_health()     ← saúde das URLs                │
│    └─ _aplicar_afiliados_health() ← status de afiliados        │
│                                                                  │
│  POST /api/editar → overrides.json + audit_log.jsonl            │
└───────────────────────┬─────────────────────────────────────────┘
                        │ JSON over HTTP
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FRONTEND (SPA)                               │
│                                                                  │
│  app.js  ─── carregarDados() ──► /api/dados  /api/stats        │
│           ── polling 15s     ──► /api/url-health                │
│           ── polling 30s     ──► /api/afiliados-health          │
│           ── filtros/tabela/gráficos (Chart.js)                 │
│           ── exportação CSV / XLSX (SheetJS)                    │
└─────────────────────────────────────────────────────────────────┘
```

### Padrão de merge em memória
Os daemons de background escrevem em arquivos JSON separados. O Flask **nunca modifica** `bets_enriquecidas.json` via API — ele apenas lê e mescla campos `_*` (prefixo underscore) em memória a cada request. Isso garante:
- Sem condições de corrida entre daemons e servidor HTTP
- Dados base sempre recuperáveis (sem mutação)
- Campos `_*` nunca são persistidos no JSON base

---

## 3. Módulos — Referência Técnica

### 3.1 `app.py` — Servidor Flask / API

**Responsabilidade:** Ponto de entrada HTTP, gerenciamento dos dados em memória, API REST, inicialização dos workers.

#### Estado global
```python
_dados: list[dict]          # registros em memória (base + merges)
_overrides: dict[str, dict] # edições manuais indexadas por CNPJ
_lock_overrides: Lock       # mutex para escrita de overrides
_AUDIT_LOCK: Lock           # mutex para append no JSONL de auditoria
```

#### Funções de merge (pipeline de carregamento)
```
recarregar_dados()
  ├── _carregar_dados()           → lê JSON ou CSV de fallback
  ├── _aplicar_overrides()        → mescla edições manuais; define _editado_manualmente
  ├── _aplicar_url_health()       → mescla url_health.json; define _url_health_status, _url_inativa
  └── _aplicar_afiliados_health() → mescla afiliados_health.json; define _afiliados_display
```

#### Campos editáveis via `/api/editar`
```python
CAMPOS_EDITAVEIS = {
    "email_contato", "url", "marca", "razao_social", "cnpj",
    "uf", "municipio", "url_afiliados", "observacao"
}
```

#### Lógica de prioridade de overrides
```
url_afiliados editado manualmente
  → _afiliados_status = "encontrado_manual"
  → _afiliados_display = "sim"          ← tem precedência sobre o daemon

email_contato editado manualmente
  → status = "encontrado_manual"        ← tem precedência sobre scraping
```

#### Audit log (`dados/audit_log.jsonl`)
Cada edição gera uma linha JSON:
```json
{"ts": "2026-05-02T14:30:00", "acao": "edit", "cnpj": "12345678000100",
 "campo": "email_contato", "valor_anterior": null, "valor_novo": "contato@bet.com", "ip": "127.0.0.1"}
```
Ações possíveis: `edit`, `delete`, `reset`

---

### 3.2 `pipeline.py` — Pipeline de coleta completo

**Responsabilidade:** Orquestra coleta de emails, enriquecimento CNPJ e coleta de afiliados em modo batch (CLI, não usado pelo servidor Flask).

#### Fluxo de execução
```
1. Carrega CSV do gov.br (ou arquivo local via --csv)
2. coletar_emails_paralelo()    → MAX_WORKERS_EMAIL=5, com checkpoint
3. enriquecer_cnpj_paralelo()   → MAX_WORKERS_CNPJ=3 (limitado por ReceitaWS)
4. coletar_afiliados_paralelo() → MAX_WORKERS_AFILIADOS=4 (opcional, --com-afiliados)
5. Salva bets_enriquecidas.json + bets_com_emails.csv + relatorio.txt
```

#### Argumentos CLI
| Flag | Descrição |
|---|---|
| `--reiniciar` | Zera checkpoint, reprocessa tudo |
| `--so-cnpj` | Pula coleta de email, só enriquece CNPJ |
| `--so-afiliados` | Carrega JSON existente e coleta só afiliados |
| `--com-afiliados` | Ativa coleta de afiliados (desativada por padrão) |
| `--limite N` | Processa apenas N registros (testes) |
| `--csv caminho` | Usa CSV local em vez de baixar |

#### Checkpoint
`checkpoint.json` armazena CNPJs já processados por fase. Em caso de interrupção, o pipeline retoma de onde parou.

---

### 3.3 `csv_sync.py` — Sincronização com CSV oficial

**Responsabilidade:** Mantém `bets_enriquecidas.json` sincronizado com a lista oficial do gov.br.

#### Lógica de reconciliação
```
Chave composta: (cnpj_limpo, url_normalizada)
├── Novo par → adiciona registro, agenda enriquecimento
├── URL mudou → atualiza url + marca _url_atualizada = True
├── Par removido → marca _removido_do_csv = True (não deleta)
└── Par retorna → limpa _removido_do_csv
```

#### Forward-fill de CNPJs
O CSV gov.br agrupa múltiplas marcas da mesma empresa em linhas consecutivas onde `razao_social` e `cnpj` ficam vazios nas linhas N+1. O `csv_sync` aplica `ffill` do pandas para propagá-los.

#### Arquivos de saída
- `dados/bets_enriquecidas.json` — base de dados principal
- `dados/csv_sync_status.json` — resultado da última sync (iniciado_em, finalizado_em, sucesso, adicionadas, removidas, url_atualizada)

---

### 3.4 `url_health.py` — Health-check de URLs

**Responsabilidade:** Valida continuamente se as URLs das bets estão acessíveis.

#### Parâmetros
```python
TICK_SEGUNDOS = 60        # ciclo de verificação
URLS_POR_TICK = 10        # URLs por ciclo
WORKERS = 2               # threads paralelas
INTERVALO_RE_CHECK = 180  # recheck após 3 min
```

#### Status possíveis
| Status | Descrição |
|---|---|
| `ok` | HTTP 2xx |
| `redirect` | Redirecionou para domínio diferente |
| `erro_http` | HTTP 4xx/5xx |
| `erro_conexao` | Timeout / conexão recusada |
| `erro_ssl` | Certificado inválido |
| `erro_dns` | Domínio não resolvido |
| `timeout` | Excedeu limite de tempo |
| `desconhecido` | Ainda não verificado |

#### Auto-redirect permanente
Quando `AUTO_APLICAR_REDIRECT_PERMANENTE = True`, redirects 301/308 são automaticamente aplicados como overrides em `overrides.json`, atualizando a URL canônica da bet.

#### Arquivo de saída
`dados/url_health.json` — `{url: {status, http_code, checado_em, latencia_ms, redirecionou, url_final}}`

---

### 3.5 `afiliados_health.py` — Detecção de programas de afiliados

**Responsabilidade:** Detecta automaticamente se cada bet possui programa de afiliados.

#### Parâmetros
```python
TICK_SEGUNDOS = 60         # ciclo
URLS_POR_TICK = 5          # URLs por ciclo (mais lento que url_health — scraping completo)
WORKERS = 3                # threads paralelas
INTERVALO_RE_CHECK = 300   # recheck após 5 min
```

#### Status possíveis (retornados por `coletar_afiliados`)
| Status raw | `_afiliados_display` | Significado |
|---|---|---|
| `encontrado_completo` | `sim` | URL + email de afiliados encontrados |
| `encontrado_url` | `sim` | Só URL do programa |
| `encontrado_email` | `sim` | Só email de afiliação |
| `nao_encontrado` | `nao` | Site acessado, nenhum programa detectado |
| `bloqueado_robots` | `nao` | robots.txt bloqueou acesso |
| `erro_conexao` | `nao_encontrado` | Falha de conexão |
| `sem_url` | `nao_encontrado` | Registro sem URL |
| `encontrado_manual` | `sim` | Override manual via dashboard |

#### Arquivo de saída
`dados/afiliados_health.json` — `{url: {detectado, status, url_afiliado, email_afiliado, checado_em}}`

**Nota crítica:** A chave é a **URL da bet** (não o CNPJ). O merge em `_aplicar_afiliados_health()` usa `r.get("url")` como chave de lookup.

---

### 3.6 `coletar_bets.py` — Scraper de emails

**Responsabilidade:** Coleta emails de contato nos sites das bets via scraping.

#### Estratégia de coleta (4 fases)
```
Fase 1: Baixa home → extrai emails diretos e links úteis
Fase 2: Tenta subpáginas (contato, sobre, ajuda...)
Fase 3: Playwright se site é SPA e sem resultado
Fase 4: Normaliza e ranqueia candidatos de email
```

#### Hierarquia de fontes de email
```
JSON-LD/Schema.org (estruturado, alta confiança)
  → Links mailto: no HTML
    → Rodapé da página
      → Regex no texto corrido
```

#### Dependências opcionais
```python
try: from bs4 import BeautifulSoup; _BS4_OK = True
except: _BS4_OK = False  # fallback: regex direto no HTML

try: from curl_cffi import requests; CFFI_DISPONIVEL = True
except: CFFI_DISPONIVEL = False  # fallback: requests padrão

try: from playwright.sync_api import sync_playwright; PLAYWRIGHT_DISPONIVEL = True
except: PLAYWRIGHT_DISPONIVEL = False  # sem rendering JS
```

---

### 3.7 `coletar_afiliados.py` — Scraper de programas de afiliados

**Responsabilidade:** Detecta links/emails de programas de afiliados em cada bet.

#### Estratégia (4 fases)
```
Fase 1a : Home → extrai âncoras com score de afiliação
Fase 1a.5: Playwright na home se site é SPA (sem sinais nos links)
Fase 1b : Tenta 30+ subpaths diretos (/afiliados, /affiliates, /parceiros...)
Fase 2  : Baixa página de afiliados → extrai email dedicado
Fase 3  : Playwright na página de afiliados se retornou HTML vazio
```

#### Sistema de scoring para candidatos de URL
```
+3 → Rede de afiliados conhecida (income-access.com, netrefer.com, etc.)
+3 → Subdomínio do tipo partners.brand.com / afiliados.brand.com
+2 → Path contém 'afiliado', 'affiliate', 'partner'...
+2 → Texto da âncora é explicitamente de afiliação
Threshold: score >= 2 para ser candidato
```

#### Redes de afiliados conhecidas (whitelabels detectados)
`income-access.com`, `netrefer.com`, `myaffiliates.com`, `affsource.com`, `smartico.ai`, `affilka.com`, `scaleo.io`, `trackier.com`, `betaffiliates.com.br`, `goldenpartners.com`, entre outros.

#### bs4 lazy import
```python
def _descobrir_candidatos_url(html, url_base):
    try:
        from bs4 import BeautifulSoup  # lazy import
    except ImportError:
        # fallback: regex href=[...] no HTML bruto
        ...
        return sorted(encontrados.items(), ...)
    soup = BeautifulSoup(html, "html.parser")
    # path normal com bs4
```

---

### 3.8 `enriquecer_cnpj.py` — Enriquecimento de dados empresariais

**Responsabilidade:** Consulta APIs externas para obter dados completos da empresa (CNPJ).

#### Cadeia de consultas
```
BrasilAPI (prioridade, sem rate limit rigoroso)
  → ReceitaWS (fallback, 3 req/min global)
    → Scraping do site da bet (regime tributário no footer)
```

#### Campos enriquecidos
```python
{
    "uf": "SP",
    "municipio": "São Paulo",
    "capital_social": 1000000.0,
    "data_abertura": "2020-01-01",
    "situacao_cadastral": "ATIVA",
    "porte_empresa": "DEMAIS",          # MEI / ME / EPP / DEMAIS
    "regime_tributario": "Lucro Real",  # inferido
    "natureza_juridica": "Sociedade Limitada",
    "logradouro": "...", "numero": "...", "cep": "...",
    "fonte_regime": "BrasilAPI",        # rastreabilidade
    "confiabilidade_dado": "alta"       # alta / media / baixa
}
```

#### Cache interno
- TTL: 30 dias
- Chave: CNPJ normalizado (só dígitos)
- Evita chamadas duplicadas quando várias marcas compartilham o mesmo CNPJ

#### Inferência de regime tributário
```
MEI (porte=MEI)        → Simples Nacional (MEI)
Simples=True           → Simples Nacional
Lucro Real             → DEMAIS + flag_lucro_real
Lucro Presumido        → padrão para DEMAIS
Imune/Isento           → flags específicas da Receita
```

---

### 3.9 `enriquecer_base.py` — Enriquecimento batch do JSON

**Responsabilidade:** Preenche campos faltantes em `bets_enriquecidas.json` sem precisar rodar o pipeline completo.

#### Uso programático
```python
from enriquecer_base import enriquecer_registros_pendentes
enriquecer_registros_pendentes(force=False, limite=None)
# force=True: re-enriquece todos, mesmo os já preenchidos
```

#### Agrupamento por CNPJ
Registros com mesmo CNPJ são enriquecidos uma única vez e o resultado é aplicado a todos eles (uma bet pode ter múltiplas marcas / CNPJs compartilhados).

---

### 3.10 `json_store.py` — I/O atômico de JSON

**Responsabilidade:** Escrita segura de arquivos JSON (usado por vários módulos).

#### Padrão de escrita atômica
```python
salvar(caminho, data):
    tmp = caminho.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))   # 1. escreve no tmp
    caminho.rename(".json.bak")        # 2. backup da versão anterior
    tmp.rename(caminho)                # 3. rename atômico (OS-level)
```

#### Leitura com fallback
```python
ler(caminho):
    try: return json.loads(caminho.read_text())
    except JSONDecodeError:
        bak = caminho.with_suffix(".json.bak")
        if bak.exists(): return json.loads(bak.read_text())
    return default
```

#### Locks por arquivo
```python
_LOCKS: dict[Path, RLock] = {}  # lock diferente por arquivo
```
Previne que dois threads escrevam no mesmo arquivo simultaneamente.

---

### 3.11 `logging_config.py` — Logging centralizado

**Responsabilidade:** Configura logging estruturado para todo o projeto.

#### Configuração
```
Console → WARNING (ou env LOG_LEVEL) → formato texto legível
Arquivo → DEBUG → logs/app.log → JSON Lines (rotação 10 MB, 5 backups)
```

#### Formato JSON (arquivo)
```json
{"ts": "2026-05-02T14:30:00.123", "level": "INFO", "logger": "app",
 "msg": "audit", "acao": "edit", "cnpj": "...", "campo": "email_contato"}
```

#### Uso
```python
from logging_config import get_logger
logger = get_logger(__name__)
logger.info("mensagem", extra={"campo": "valor"})
```

Bibliotecas silenciadas: `urllib3`, `requests`, `werkzeug`, `charset_normalizer`.

---

## 4. Frontend — Design System

### 4.1 `templates/index.html`

Estrutura HTML semântica de SPA (Single Page Application):

```html
<body>
  <aside class="sidebar">          <!-- navegação fixa lateral -->
    <div class="sidebar-brand">    <!-- logo + nome -->
    <nav class="sidebar-nav">      <!-- links de navegação -->
    <div class="sidebar-footer">   <!-- crédito de dados -->
  </aside>

  <div class="main-wrapper">       <!-- área principal (margin-left: 160px) -->
    <header class="topbar">        <!-- breadcrumb + ações -->
    <section class="kpi-section">  <!-- 8 cards KPI com sparklines SVG -->
    <div class="filter-bar">       <!-- filtros horizontais -->
    <main class="content">
      <div class="table-wrapper">  <!-- tabela paginada 13 colunas -->
      <div class="pagination">     <!-- controles de paginação -->
      <section class="charts-section"> <!-- 3 gráficos Chart.js -->
    </main>
    <footer class="footer">
  </div>
</body>
```

#### KPI Cards (8 cards)
| ID | Cor | Métrica |
|---|---|---|
| `kpi-total` | laranja | Total de Bets Regulamentadas |
| `kpi-email` | verde | Com Email encontrado |
| `kpi-sem-email` | vermelho | Sem Email |
| `kpi-afiliados` | roxo | Com Afiliados detectados |
| `kpi-urls-ativas` | verde | URLs ativas (HTTP 2xx) |
| `kpi-urls-inativas` | vermelho | URLs inacessíveis |
| `kpi-editados` | roxo | Editados manualmente |
| `kpi-atualizacao` | azul | Data da última coleta |

Cada card tem um SVG sparkline estático decorativo com `<polyline>`.

---

### 4.2 `static/style.css` — Design System

#### Variáveis CSS (tema escuro)
```css
:root {
  --bg: #0d0d1a;           /* fundo principal */
  --bg2: #12121f;          /* fundo secundário */
  --card: #16162a;         /* fundo de cards */
  --sidebar-w: 160px;      /* largura da sidebar */
  --accent: #3b82f6;       /* azul accent */
  --success: #22c55e;      /* verde */
  --danger: #ef4444;       /* vermelho */
  --warning: #f59e0b;      /* amarelo */
  --purple: #a855f7;       /* roxo */
  --orange: #fb923c;       /* laranja */
}
```

#### Layout principal
```css
body { display: flex; }                          /* sidebar + wrapper lado a lado */
.sidebar { position: fixed; width: 160px; }     /* sidebar fixa */
.main-wrapper { margin-left: 160px; flex: 1; } /* conteúdo com offset */
```

#### Componentes de status
```css
/* Badges de status genéricos */
.badge { display: inline-flex; border-radius: 999px; }
.badge-success { background: rgba(34,197,94,.15); color: #22c55e; }
.badge-danger  { background: rgba(239,68,68,.15);  color: #ef4444; }
.badge-warning { background: rgba(251,191,36,.15); color: #fbbf24; }
.badge-neutral { background: rgba(148,163,184,.1); color: #94a3b8; }

/* Dots de saúde de URL */
.url-dot { width: 8px; height: 8px; border-radius: 50%; }
.url-dot-ok          { background: #22c55e; }
.url-dot-redirect    { background: #fbbf24; }
.url-dot-erro        { background: #ef4444; }
.url-dot-desconhecido{ background: #475569; }

/* Célula de afiliados */
.afiliados-cell { display: flex; justify-content: center; }
.afiliados-dot-sim { color: #22c55e; }
.afiliados-dot-nao { color: #ef4444; }
.afiliados-dot-nd  { color: #64748b; }
```

#### Edição inline
```css
.cell-editable   /* células clicáveis para edição */
.cell-empty      /* estado vazio com "+ adicionar" */
.inline-edit-input   /* input de edição */
.inline-edit-save    /* botão ✓ */
.inline-edit-cancel  /* botão ✕ */
.obs-textarea        /* textarea de observações (resize vertical) */
```

---

### 4.3 `static/app.js` — Lógica de frontend

#### Estado global
```javascript
let todosOsDados = [];     // todos os registros da API
let dadosFiltrados = [];   // subconjunto após filtros
let paginaAtual = 1;
let tamanhoPagina = 25;    // 25 | 50 | 100
let colunaOrdem = '';
let ordemAsc = true;
```

#### Fluxo de inicialização
```javascript
DOMContentLoaded → carregarDados()
                 → iniciarPollingUrlHealth()    // polling 15s
                 → iniciarPollingAfiliadosHealth() // polling 30s
```

#### Ciclo de dados
```
carregarDados()
  ├── fetch /api/dados     → todosOsDados
  ├── fetch /api/stats     → preencherKPIs() + preencherFiltrosDropdown()
  ├── dadosFiltrados = [...todosOsDados]
  ├── renderizarTabela()
  └── renderizarGraficos()
```

#### Sistema de filtros
Todos os filtros são aplicados client-side sobre `todosOsDados`:

| Filtro | Campo | Tipo |
|---|---|---|
| Busca por marca | `marca` | texto livre |
| Status email | `status` | grupo STATUS_GROUPS |
| Afiliados | `_afiliados_display` | `sim / nao / nao_encontrado` |
| Saúde URL | `_url_health_status` | `ok / redirect / erro / desconhecido` |
| Porte | `porte_empresa` | enum |
| Situação | `situacao_cadastral` | enum |
| UF | `uf` | sigla |
| Município | `municipio` | cascata após UF |
| Data coleta (de/até) | `data_coleta` | ISO date |

#### Tabela (13 colunas)
```
Marca | Razão Social | CNPJ | URL (+ dot saúde) | Email | Afiliado (badge) |
UF | Município | Status (badge) | Capital Social | Abertura | Coleta | Observação
```

#### Funções de renderização de célula
```javascript
celulaEditavel(r, campo, tipo)   // célula clicável para edição inline
celulaObservacao(r)              // textarea multi-linha para notas
badgeStatus(r.status)            // badge Com email / Sem email / Falhou
badgeAfiliados(r)                // badge Sim / Não / N/E com link
urlHealthDot(r)                  // ● colorido com tooltip detalhado
```

#### Edição inline
1. Click em `.cell-editable` → `iniciarEdicaoInline(el)`
2. Input criado via DOM API (não innerHTML)
3. Enter ou ✓ → `POST /api/editar`
4. `ressincronizar()` → re-fetch dados + stats, preserva filtros
5. Flash visual na linha salva

#### Exportação
- **CSV:** colunas fixas + `﻿` BOM para Excel
- **XLSX:** SheetJS com autofit de colunas (max 40 chars)

#### Polling de URL health (15s)
Atualiza apenas as `.url-dot` no DOM — não re-renderiza a tabela inteira.

#### Polling de afiliados health (30s)
Atualiza `_afiliados_display` em memória. Só re-renderiza a tabela se houve mudança, e só se o filtro de afiliados não estiver ativo (nesse caso, reaplica `aplicarFiltros()`).

---

## 5. APIs — Referência de Endpoints

### GET `/`
Retorna o template `index.html`. Sem cache server-side (debug=False + Jinja2 caching interno).

---

### GET `/api/dados`
Retorna todos os registros com merges aplicados (url_health + afiliados_health).

**Response:** `200 OK` — `application/json`
```json
[
  {
    "marca": "BetX",
    "razao_social": "BetX Apostas Ltda",
    "cnpj": "12.345.678/0001-90",
    "url": "https://betx.bet.br",
    "email_contato": "contato@betx.bet.br",
    "status": "encontrado",
    "url_afiliados": "",
    "uf": "SP",
    "municipio": "São Paulo",
    "capital_social": 1000000.0,
    "data_abertura": "2020-01-15",
    "situacao_cadastral": "ATIVA",
    "porte_empresa": "DEMAIS",
    "regime_tributario": "Lucro Real",
    "data_coleta": "2026-04-01",
    "observacao": "Verificar novo email",
    "_url_health_status": "ok",
    "_url_http_code": 200,
    "_url_inativa": false,
    "_afiliados_display": "sim",
    "_afiliados_url": "https://betx.bet.br/afiliados",
    "_editado_manualmente": false
  }
]
```

**Campos `_*`** são calculados em memória — nunca persistidos no JSON base.

---

### GET `/api/stats`
KPIs e distribuições para os cards e dropdowns.

**Response:**
```json
{
  "total": 186,
  "com_email": 120,
  "sem_email": 45,
  "com_afiliados": 3,
  "afiliados_sim": 3,
  "afiliados_nao": 3,
  "afiliados_desconhecido": 180,
  "editados_manualmente": 5,
  "ultima_atualizacao": "2026-04-01",
  "portes": ["DEMAIS", "EPP", "ME", "MEI"],
  "situacoes": ["ATIVA", "BAIXADA", "INAPTA"],
  "ufs": ["MG", "RJ", "SP"],
  "urls_ativas": 140,
  "urls_redirect": 12,
  "urls_inativas": 8,
  "urls_desconhecido": 26,
  "csv_sync": {
    "ultimo_sync": "2026-05-02T08:00:00",
    "sucesso": true,
    "adicionadas": 2,
    "removidas": 0,
    "url_atualizada": 1
  }
}
```

---

### POST `/api/editar`
Edita um campo de um registro e persiste em `overrides.json`.

**Request body:**
```json
{"cnpj": "12345678000190", "campo": "email_contato", "valor": "novo@email.com"}
```

**Semântica do campo `valor`:**
| Valor | Efeito |
|---|---|
| `"string não-vazia"` | Armazena o valor como override |
| `""` (string vazia) | Deleta explicitamente (mascara o valor base) |
| `null` | Reseta — remove o override, volta ao valor base |

**Response:**
```json
{"ok": true, "registro": {...registro atualizado...}}
```

**Erros possíveis:**
```json
{"ok": false, "erro": "CNPJ ausente."}
{"ok": false, "erro": "Campo 'xyz' não é editável.", "editaveis": [...]}
{"ok": false, "erro": "Email inválido."}
```

---

### GET `/api/url-health`
Retorna o estado atual de saúde de todas as URLs.

**Response:** `{url: {status, http_code, checado_em, latencia_ms, redirecionou, url_final}}`

---

### GET `/api/afiliados-health`
Retorna o estado atual de detecção de afiliados.

**Response:** `{url: {detectado, status, url_afiliado, email_afiliado, checado_em}}`

---

### GET `/api/municipios/<uf>`
Retorna municípios disponíveis para a UF (cascata de filtros).

**Response:** `["Belo Horizonte", "Contagem", ...]`

---

### GET `/api/csv-sync-status`
Status da última sincronização com o CSV do gov.br.

---

### POST `/api/csv-sync-agora`
Força sincronização imediata (sem aguardar o ciclo de 6h). Retorna resultado + recarrega dados.

---

### POST `/api/recarregar`
Força recarga dos dados em memória sem reiniciar o servidor.

**Response:** `{"ok": true, "total": 186}`

---

### GET `/api/audit-log?limite=200`
Retorna as últimas N entradas do audit log em ordem cronológica reversa.

---

## 6. Fluxo de Dados

### 6.1 Ciclo de vida de um registro

```
gov.br CSV
    │
    ▼ csv_sync.py (6h)
bets_enriquecidas.json
├── marca, razao_social, cnpj, url, data_coleta
    │
    ▼ enriquecer_cnpj.py (pipeline ou enriquecer_base)
    ├── uf, municipio, capital_social, data_abertura
    ├── situacao_cadastral, porte_empresa, regime_tributario
    ├── logradouro, cep, municipio, natureza_juridica
    │
    ▼ coletar_bets.py (pipeline)
    ├── email_contato, status
    │
    ▼ coletar_afiliados.py (pipeline ou afiliados_health.py)
    └── url_afiliados, status_afiliados
```

### 6.2 Merge em memória (por request)

```
bets_enriquecidas.json (base imutável)
    +
overrides.json          → campos editados manualmente têm prioridade absoluta
    +
url_health.json         → _url_health_status, _url_http_code, _url_inativa
    +
afiliados_health.json   → _afiliados_display, _afiliados_url, _afiliados_status
    =
_dados (em memória, retornado por /api/dados)
```

### 6.3 Edição manual

```
Frontend input
    │ POST /api/editar
    ▼
overrides.json (persistido atomicamente)
    +
audit_log.jsonl (append-only)
    │
    ▼ recarregar_dados()
_dados (atualizado em memória)
    │
    ▼ resposta JSON
Frontend ressincronizar()
```

---

## 7. Estrutura de Arquivos

```
projeto bet/
├── app.py                     # Servidor Flask + API + estado global
├── pipeline.py                # Pipeline de coleta completo (CLI)
├── csv_sync.py                # Sincronização com CSV gov.br
├── url_health.py              # Daemon de health-check de URLs
├── afiliados_health.py        # Daemon de detecção de afiliados
├── coletar_bets.py            # Scraper de emails
├── coletar_afiliados.py       # Scraper de programas de afiliados
├── enriquecer_cnpj.py         # Consulta BrasilAPI / ReceitaWS
├── enriquecer_base.py         # Enriquecimento batch do JSON
├── json_store.py              # I/O atômico de JSON (thread-safe)
├── logging_config.py          # Configuração centralizada de logs
├── _start_server.py           # Entry point de produção (porta 5002)
│
├── templates/
│   └── index.html             # SPA template único
│
├── static/
│   ├── app.js                 # Toda lógica frontend (~1100 linhas)
│   └── style.css              # Design system (~895 linhas)
│
├── dados/                     # Criado automaticamente
│   ├── bets_enriquecidas.json # Base principal (186 registros)
│   ├── overrides.json         # Edições manuais
│   ├── url_health.json        # Saúde das URLs
│   ├── afiliados_health.json  # Status de afiliados
│   ├── csv_sync_status.json   # Status da última sync
│   └── audit_log.jsonl        # Log de auditoria (append-only)
│
├── logs/                      # Criado automaticamente
│   └── app.log                # JSON Lines rotativo (10 MB × 5)
│
├── bets_com_emails.csv        # Fallback CSV (gerado pelo pipeline)
├── checkpoint.json            # Estado do pipeline (retomada)
└── relatorio.txt              # Relatório da última execução do pipeline
```

---

## 8. Dados e Schemas

### 8.1 Schema de registro em `bets_enriquecidas.json`

```json
{
  // Campos base (CSV gov.br)
  "marca": "string",
  "razao_social": "string",
  "cnpj": "string (com máscara XX.XXX.XXX/XXXX-XX)",
  "url": "string (https://...)",
  "data_coleta": "string (YYYY-MM-DD)",

  // Enriquecimento CNPJ
  "uf": "string (sigla 2 letras)",
  "municipio": "string",
  "capital_social": "float",
  "data_abertura": "string (YYYY-MM-DD)",
  "situacao_cadastral": "string (ATIVA | BAIXADA | INAPTA | SUSPENSA)",
  "porte_empresa": "string (MEI | ME | EPP | DEMAIS)",
  "regime_tributario": "string",
  "natureza_juridica": "string",
  "logradouro": "string",
  "numero": "string",
  "complemento": "string",
  "bairro": "string",
  "cep": "string",
  "pais": "string",
  "fonte_regime": "string",
  "confiabilidade_dado": "string (alta | media | baixa)",

  // Coleta de emails
  "email_contato": "string",
  "status": "string (encontrado | encontrado_js | encontrado_manual | nao_encontrado | erro_conexao | bloqueado_robots | sem_url)",

  // Afiliados (pipeline batch)
  "url_afiliados": "string",
  "status_afiliados": "string",
  "email_afiliado": "string",

  // Controle interno
  "observacao": "string",
  "_removido_do_csv": "boolean",
  "_removido_em": "string (ISO datetime)",
  "_enriquecido_em": "string (ISO datetime)"
}
```

### 8.2 Schema de `overrides.json`

```json
{
  "_schema_version": 1,
  "_salvo_em": "2026-05-02T14:00:00",
  "12345678000190": {
    "email_contato": "novo@email.com",
    "url_afiliados": "https://bet.com/afiliados",
    "_edited_at": "2026-05-02T14:00:00"
  }
}
```

### 8.3 Schema de `url_health.json`

```json
{
  "https://betx.bet.br": {
    "status": "ok",
    "http_code": 200,
    "checado_em": "2026-05-02T14:00:00",
    "latencia_ms": 342,
    "redirecionou": false,
    "url_final": ""
  }
}
```

### 8.4 Schema de `afiliados_health.json`

```json
{
  "https://betx.bet.br": {
    "detectado": true,
    "status": "encontrado_url",
    "url_afiliado": "https://betx.bet.br/afiliados",
    "email_afiliado": "",
    "checado_em": "2026-05-02T14:00:00"
  }
}
```

---

## 9. Workers em Background

### Inicialização dos workers

Os workers são iniciados em `app.py` na condição:
```python
if _deve_iniciar_worker():   # evita duplicação sob reloader do Flask debug
    url_health.iniciar_worker()
    csv_sync.iniciar_worker()
    if _AFILIADOS_HEALTH_DISPONIVEL:
        afiliados_health.iniciar_worker()
```

Todos os workers são **daemon threads** — morrem automaticamente quando o processo principal encerra.

### Tabela de workers

| Worker | Módulo | Ciclo | Unidades/ciclo | Threads | Re-check |
|---|---|---|---|---|---|
| URL Health | url_health.py | 60s | 10 URLs | 2 | 180s |
| Afiliados Health | afiliados_health.py | 60s | 5 URLs | 3 | 300s |
| CSV Sync | csv_sync.py | 6h | CSV completo | 1 | — |

### Priorização de verificação
Todos os workers usam `_selecionar_fatia()` com lógica:
1. **Nunca checadas** → prioridade máxima (idade = ∞)
2. **Mais antigas** → priorizadas pelo timestamp `checado_em`
3. **Recentes** (dentro do `INTERVALO_RE_CHECK`) → ignoradas no ciclo atual

---

## 10. Dívidas Técnicas e Melhorias

### 4.1 Dívidas técnicas identificadas

| # | Descrição | Impacto | Esforço | Prioridade |
|---|-----------|---------|---------|------------|
| 1 | **bs4 não instalado** — scraping opera em modo degradado (regex no HTML bruto), sem parse de DOM real. Coleta de emails e detecção de afiliados perdem precisão significativa. | Alto | Baixo | P1 |
| 2 | **Sem autenticação no dashboard** — qualquer usuário na rede local pode acessar e editar dados. Sem sessão, sem login, sem proteção de rotas. | Alto | Médio | P1 |
| 3 | **`_dados` global sem lock de leitura** — múltiplos request threads leem `_dados` enquanto `recarregar_dados()` o substitui. Em Python com GIL isso raramente causa problema real, mas não é correto. | Médio | Baixo | P2 |
| 4 | **Sem validação de CNPJ no frontend** — `POST /api/editar` aceita qualquer string no campo `cnpj`. Um CNPJ inválido pode criar um override órfão. | Médio | Baixo | P2 |
| 5 | **XLSX export inclui campos `_*` internos** — `exportarXLSX()` usa `json_to_sheet(dadosFiltrados)` sem filtrar colunas, expondo `_url_health_status`, `_afiliados_display`, etc. | Baixo | Baixo | P2 |
| 6 | **Sem paginação server-side** — `/api/dados` retorna todos os 186 registros sempre. Com crescimento da base (ex.: 1000+ bets), isso se torna um gargalo. | Médio | Alto | P2 |
| 7 | **`audit_log.jsonl` cresce indefinidamente** — sem rotação ou limite de tamanho. Em uso intenso de edições, pode crescer para MBs ao longo de meses. | Baixo | Baixo | P3 |
| 8 | **Sem testes automatizados** — nenhum arquivo de teste existe. Módulos críticos como `json_store`, `_aplicar_overrides`, `_display_afiliados` não têm cobertura. | Alto | Médio | P2 |
| 9 | **ReceitaWS rate limit global com Lock thread** — o lock `_RECEITAWS_LOCK` é global ao processo, mas se o servidor Flask reiniciar no meio de uma consulta, o estado do lock é perdido. Não é reentrante por processo. | Baixo | Médio | P3 |
| 10 | **`coletar_bets.py` acoplado ao pipeline batch** — o módulo é importado por `coletar_afiliados.py` mas contém lógica não utilizada pelo daemon (ex.: `coletar_empresa`, relatórios). Sem separação de responsabilidades. | Baixo | Alto | P3 |
| 11 | **Flask rodando sem WSGI real** — usa o servidor de desenvolvimento do Werkzeug (single-threaded por padrão). Para uso com múltiplos usuários simultâneos, precisa de Gunicorn/Waitress. | Médio | Baixo | P1 |
| 12 | **Sparklines SVG são estáticas** — os `<polyline>` nas KPI cards têm pontos hardcoded, não refletem dados reais ao longo do tempo. | Baixo | Médio | P3 |
| 13 | **Sem tratamento de conflito de edição** — se dois usuários editarem o mesmo registro simultaneamente, o último a salvar sobrescreve o anterior sem aviso. | Médio | Médio | P2 |
| 14 | **Playwright opcional mas sem degradação informada** — quando Playwright não está disponível, sites JS-only simplesmente não têm email coletado, sem nenhum indicador no dashboard. | Baixo | Médio | P3 |

---

### 4.2 Melhorias de performance

#### Gargalos identificados

**1. `/api/stats` recalcula tudo a cada request**
```python
# Atual: cada GET /api/stats chama _aplicar_afiliados_health() + sum() sobre 186 registros
# Custo baixo hoje (186 items), mas cresce linearmente com a base

# Melhoria sugerida: cache de stats com TTL de 5s
_stats_cache = {"data": None, "ts": 0}
def api_stats():
    if time.time() - _stats_cache["ts"] < 5:
        return jsonify(_stats_cache["data"])
    # recalcular...
```

**2. `/api/dados` retorna payload completo a cada request**
```
186 registros × ~50 campos = ~150 KB por request
Frontend faz isso a cada: carregarDados() + ressincronizar() + polling-triggered reloads

Melhoria: ETag / Last-Modified + 304 Not Modified
Alternativa: paginação server-side GET /api/dados?pagina=1&limite=50
```

**3. Polling de URL health recalcula todos os dots no DOM**
```javascript
// Atual: itera todos os tr[data-cnpj] a cada 15s
// Melhoria: Map de CNPJ → elemento DOM para O(1) lookup
const _trMap = new Map(); // cnpj → tr element
```

**4. `_aplicar_url_health()` e `_aplicar_afiliados_health()` chamados 3× por `/api/stats`**
```python
# api_stats() chama _aplicar_url_health() e _aplicar_afiliados_health()
# depois de já terem sido aplicados em recarregar_dados()
# Pode ser otimizado com dirty flag
```

**5. `enriquecer_cnpj.py` cache em memória não sobrevive reinicialização**
O cache de 30 dias fica em memória. A cada restart do servidor (frequente em desenvolvimento), todos os dados em cache são perdidos e precisam ser re-consultados.

```python
# Melhoria: persistir cache em dados/cnpj_cache.json
```

#### Não há queries N+1
O sistema usa arquivos JSON carregados inteiros em memória — não há queries por registro individual. O padrão é: carrega arquivo completo → filtra em memória.

---

### 4.3 Melhorias de segurança

#### Dados sensíveis em logs
- O audit log (`audit_log.jsonl`) registra `valor_anterior` e `valor_novo` de qualquer campo editado, incluindo emails. **Não é um problema** para dados de contato de empresas (não são dados pessoais sensíveis neste contexto), mas é um ponto de atenção.
- Os logs em `logs/app.log` incluem IPs dos editores — adequado para auditoria, mas requer controle de acesso ao arquivo.

#### Validação de input
| Ponto | Status atual | Gap |
|---|---|---|
| Campo `cnpj` em `/api/editar` | Aceita qualquer string | Sem validação de dígitos |
| Campo `email_contato` | Valida `@` e `.` no domínio | Validação superficial |
| Campo `url` | Sem validação | Poderia aceitar `javascript:` |
| Campo `valor` (tamanho) | Sem limite | String arbitrariamente longa |
| CNPJ em query params | N/A (não há) | — |

**Recomendações:**
```python
# Validar CNPJ estruturalmente
import re
_CNPJ_RE = re.compile(r'^\d{14}$')
cnpj_limpo = re.sub(r'\D', '', cnpj)
if not _CNPJ_RE.match(cnpj_limpo): return erro

# Sanitizar URL
from urllib.parse import urlparse
parsed = urlparse(valor)
if parsed.scheme not in ('http', 'https'): return erro

# Limitar tamanho dos campos
if len(valor) > 2000: return erro
```

#### Autenticação e autorização
**Não há qualquer mecanismo de autenticação.** O dashboard está completamente aberto para qualquer acesso à porta 5002.

Para uso em produção (acesso via rede), recomenda-se:
1. Autenticação básica via Flask-Login ou HTTP Basic Auth
2. Binding apenas em `127.0.0.1` (já feito — não expõe para a rede)
3. Reverse proxy (Nginx) com autenticação na frente

#### CSRF
O endpoint `POST /api/editar` não tem proteção CSRF. Sendo uma API JSON (não form), o risco é reduzido, mas deveria ter validação de `Content-Type: application/json` + `Origin` header em produção.

---

### 4.4 Melhorias de manutenibilidade

#### Onboarding de novos devs

**Pontos positivos:**
- Cada módulo tem docstring clara no topo
- Funções públicas têm type hints
- Constantes de configuração agrupadas no topo de cada arquivo
- Padrões reutilizados (e.g., `_selecionar_fatia`, json_store)

**Gaps:**
- Não há `README.md` com instruções de setup (instalação de deps, como rodar)
- Não há `requirements.txt` — dependências estão implícitas nos imports
- O fluxo de dados entre arquivos JSON não está diagramado (agora está neste documento)
- A condição `_deve_iniciar_worker()` é não-óbvia para quem não conhece o reloader do Flask

#### Cobertura de testes
**Atualmente: zero testes automatizados.**

Módulos mais críticos que deveriam ter testes unitários:

| Módulo | Funções críticas a testar |
|---|---|
| `json_store.py` | Write atômico, fallback para .bak, locks concorrentes |
| `app.py` | `_aplicar_overrides()`, `_display_afiliados()`, validação de `/api/editar` |
| `enriquecer_cnpj.py` | Inferência de regime tributário, cache TTL |
| `csv_sync.py` | Forward-fill, reconciliação (add/remove/update) |
| `coletar_afiliados.py` | Score de candidatos URL, filtros de email |

**Setup mínimo recomendado:**
```
pytest + pytest-mock
tests/
  test_json_store.py
  test_app_overrides.py
  test_enriquecer_cnpj.py
  test_csv_sync.py
  test_afiliados_score.py
```

#### O que esta documentação cobre
Esta documentação preenche os seguintes gaps identificados:
- ✅ Diagrama de arquitetura e fluxo de dados
- ✅ Schema de todos os arquivos JSON
- ✅ Referência completa de endpoints
- ✅ Lógica de merge em memória documentada
- ✅ Dependências opcionais e fallbacks explicados
- ✅ Comportamento dos workers documentado
- ✅ Semântica dos campos `_*`

---

### 4.5 Roadmap sugerido

#### Fase 1 — Curto prazo (1–2 semanas) — Estabilidade

| # | Item | Justificativa |
|---|---|---|
| 1.1 | **Instalar bs4** (`pip install beautifulsoup4`) | Desbloqueio imediato de precisão no scraping de emails e afiliados. Impacto alto, esforço mínimo. |
| 1.2 | **Trocar Werkzeug dev server por Waitress** | `pip install waitress` + 2 linhas de código. Elimina a dívida P1 de servidor de produção. |
| 1.3 | **Adicionar validação de URL e tamanho de campo** em `/api/editar` | Previne dados inválidos no overrides.json. |
| 1.4 | **Filtrar campos `_*` do export XLSX** | Fix simples: `{k: v for k, v in r.items() if not k.startswith('_')}` |
| 1.5 | **Criar `requirements.txt`** | Facilita setup do ambiente. |
| 1.6 | **Rotação do audit_log.jsonl** | Adicionar truncagem quando > 50.000 linhas. |

#### Fase 2 — Médio prazo (1 mês) — Qualidade e segurança

| # | Item | Justificativa |
|---|---|---|
| 2.1 | **Autenticação básica no dashboard** | Flask-Login ou HTTP Basic Auth com senha única. Bloqueia acesso não autorizado. |
| 2.2 | **Suíte de testes unitários** (pytest) | Cobertura dos módulos críticos. Meta: ≥ 70% coverage nos módulos de negócio. |
| 2.3 | **Cache de stats com TTL** | Evita recálculo redundante a cada request de polling. |
| 2.4 | **Persistir cache CNPJ em disco** | Evita re-consultas desnecessárias a cada restart. |
| 2.5 | **Lock de leitura em `_dados`** | `threading.RWLock` ou `copy()` atômico para evitar race condition teórica. |
| 2.6 | **Indicador visual de "Playwright indisponível"** | Badge ou tooltip no dashboard informando limitação da coleta. |
| 2.7 | **Paginação server-side** para `/api/dados` | Preparação para escala: `GET /api/dados?pagina=1&limite=50&...filtros` |

#### Fase 3 — Longo prazo (2–3 meses) — Escala e extensibilidade

| # | Item | Justificativa |
|---|---|---|
| 3.1 | **Migrar storage para SQLite** | JSON files não escalam para 10.000+ registros. SQLite mantém a simplicidade sem servidor externo. Schema já está implícito nos JSONs. |
| 3.2 | **Separar `coletar_bets.py` em módulos menores** | `http_client.py`, `email_extractor.py`, `robots.py` — melhora testabilidade e reúso. |
| 3.3 | **Sparklines dinâmicas nos KPI cards** | Armazenar histórico diário de totais e renderizar via Chart.js mini. |
| 3.4 | **API de exportação server-side** | `GET /api/exportar?formato=xlsx&...filtros` — suporta bases maiores que a memória do browser. |
| 3.5 | **Sistema de notificações** | Alertar (email/webhook) quando uma bet sai do CSV oficial ou quando URL fica inativa por X horas. |
| 3.6 | **Dashboard de logs e auditoria** | Página dedicada para visualizar `audit_log.jsonl` com filtros por usuário/campo/período. |
| 3.7 | **Detecção de duplicatas de email** | Identificar emails genéricos (info@, contato@) que aparecem em múltiplas bets — pode indicar dado de baixa qualidade. |
| 3.8 | **Integração com Playwright cloud (Browserless)** | Para sites JS-only sem precisar de Playwright local — remove dependência de instalação local. |

---

*Documentação gerada por Claude — Prospector Bets v2.0 — 2026-05-02*
