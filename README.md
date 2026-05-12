# Prospector Bets — Ambiente de Desenvolvimento (DEV)

> **🧪 Este é um fork de desenvolvimento.** O sistema principal continua rodando em produção em outra pasta (`projeto bet/`) na porta **5002**.
>
> Este fork roda em **porta 5003** para coexistir e permitir validação isolada de novas features antes de mergear para o principal.

## Origem

Forked do `projeto bet/` em **2026-05-07**.

Repositório do principal: https://github.com/comercialjbernardes-ux/Venda-feita

## Como rodar

```powershell
# Da raiz do fork:
C:\PythonPortable\python312\python.exe _start_server.py
# → http://127.0.0.1:5003/
```

## Roadmap de evolução

Veja `MIGRACAO.md` para o plano de etapas e estado atual de cada uma.

Etapas planejadas:
- **Etapa 1:** Quick-wins técnicos (`/health` endpoint, cache, rate limiting, circuit breaker)
- **Etapa 2:** Modularização (`data_manager.py`, `audit.py`, type hints)
- **Etapa 3:** Health Score composto + snapshots históricos
- **Etapa 4:** Alertas inteligentes via webhook (reutiliza `notificacoes.py`)
- **Etapa 5:** AI Chat sobre os dados (Claude API + tool use)
- **Etapa 6:** Detecção de anomalias + grupos empresariais
- **Etapa 7:** Email validation

## Diferenças do principal

Esta cópia tem mudanças incrementais comparada com o principal. Veja `MIGRACAO.md` para o diff completo de cada etapa.

## Merge-back para produção

Quando estável, este fork será mergeado para o repositório principal (`Venda-feita`). Veja a seção "Merge-back" em `MIGRACAO.md`.

## Sobre o projeto

Dashboard de inteligência comercial para bets regulamentadas no Brasil.
Dados oficiais: Ministério da Fazenda / Secretaria de Prêmios e Apostas (Lei 14.790/23).
