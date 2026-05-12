# MIGRACAO.md — Rastreamento de Mudanças vs `projeto bet/` Principal

Documento que registra **o que mudou neste fork** em relação ao sistema em produção, para guiar o merge-back ao final.

---

## Status das Etapas

| Etapa | Descrição | Status | Validado pelo usuário | Commit |
|---|---|---|---|---|
| **0** | Criar fork + GitHub repo + porta 5003 | 🟡 Em andamento | — | — |
| **1** | Quick-wins técnicos (`/health`, cache, circuit breaker, rate limit) | ⚪ Pendente | — | — |
| **2** | Modularização (`data_manager.py`, `audit.py`, type hints) | ⚪ Pendente | — | — |
| **3** | Health Score composto + snapshots | ⚪ Pendente | — | — |
| **4** | Alertas inteligentes via webhook | ⚪ Pendente | — | — |
| **5** | AI Chat sobre os dados (Claude API) | ⚪ Pendente | — | — |
| **6** | Anomalias + grupos empresariais | ⚪ Pendente | — | — |
| **7** | Email validation worker | ⚪ Pendente | — | — |

Legenda: ✅ Concluída · 🟡 Em andamento · ⚪ Pendente · ❌ Bloqueada

---

## Arquivos modificados neste fork

### Etapa 0 — Criação do fork

- `_start_server.py` — porta padrão alterada de `5002` para `5003`; docstring atualizada
- `README.md` — substituído pelo README do fork (DEV)
- `MIGRACAO.md` — **NOVO**, este arquivo

### Etapa 1 — (a preencher após execução)

### Etapa 2 — (a preencher após execução)

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
