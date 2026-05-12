# executar_coleta_emails.ps1
# Script para executar a coleta de emails em producao com seguranca.
# Uso:
#   .\executar_coleta_emails.ps1               - coleta completa
#   .\executar_coleta_emails.ps1 -Limite 20    - teste com 20 primeiros
#   .\executar_coleta_emails.ps1 -Reiniciar    - ignora checkpoint e reprocessa tudo
#   .\executar_coleta_emails.ps1 -SoCNPJ       - apenas enriquecimento de CNPJ

param(
    [int]$Limite = 0,
    [switch]$Reiniciar,
    [switch]$SoCNPJ,
    [switch]$ComAfiliados
)

$PYTHON = "C:\PythonPortable\python312\python.exe"
$DIR    = "C:\Users\Administrator\Documents\venda feita\projeto bet"
$LOG    = "$DIR\logs\coleta_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

if (-not (Test-Path "$DIR\logs")) { New-Item -ItemType Directory "$DIR\logs" | Out-Null }

Write-Host ""
Write-Host "=== PROSPECTOR BETS — PIPELINE DE COLETA ===" -ForegroundColor Cyan
Write-Host "Iniciado em: $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')"
Write-Host "Log: $LOG"
Write-Host ""

Set-Location $DIR

# Verificar estado atual da base
Write-Host "--- Estado atual da base ---" -ForegroundColor Yellow
& $PYTHON -c @"
import json
try:
    d = json.load(open('dados/bets_enriquecidas.json', encoding='utf-8'))
    com_email = sum(1 for x in d if x.get('email_contato','').strip())
    com_uf    = sum(1 for x in d if x.get('uf','').strip())
    print(f'Total: {len(d)} | Com email: {com_email} | Com UF/municipio: {com_uf}')
except Exception as e:
    print(f'Erro ao ler base: {e}')
"@
Write-Host ""

# Montar argumentos do pipeline
$args_pipeline = @()
if ($Limite -gt 0)   { $args_pipeline += "--limite", $Limite }
if ($Reiniciar)      { $args_pipeline += "--reiniciar" }
if ($SoCNPJ)         { $args_pipeline += "--so-cnpj" }
if ($ComAfiliados)   { $args_pipeline += "--com-afiliados" }

Write-Host "--- Executando pipeline ---" -ForegroundColor Yellow
Write-Host "Argumentos: $($args_pipeline -join ' ')"
Write-Host ""

# Executar pipeline (saida simultaneamente em console e log)
& $PYTHON pipeline.py @args_pipeline 2>&1 | Tee-Object -FilePath $LOG

Write-Host ""
Write-Host "--- Resultado final ---" -ForegroundColor Yellow
& $PYTHON -c @"
import json
from collections import Counter
try:
    d = json.load(open('dados/bets_enriquecidas.json', encoding='utf-8'))
    com_email = sum(1 for x in d if x.get('email_contato','').strip())
    com_uf    = sum(1 for x in d if x.get('uf','').strip())
    status    = Counter(x.get('status','') for x in d)
    print(f'Total: {len(d)} | Com email: {com_email} ({100*com_email//len(d) if d else 0}%) | Com UF: {com_uf}')
    print('Status:')
    for k, v in sorted(status.items(), key=lambda x: -x[1]):
        print(f'  {k or "(vazio)":30s}: {v}')
except Exception as e:
    print(f'Erro ao ler resultado: {e}')
"@

Write-Host ""
Write-Host "=== CONCLUIDO em: $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss') ===" -ForegroundColor Green
Write-Host "Log salvo em: $LOG"
Write-Host ""
