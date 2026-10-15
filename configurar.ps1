<#
.SYNOPSIS
Instala Python 3.12, dependências fixadas, dados e modelo do Formula Lab no Windows.
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\configurar.ps1
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\configurar.ps1 -SemModelo -SemDados -SemVerificarGPU
#>
[CmdletBinding()]
param(
    [string]$Ambiente = ".venv",
    [switch]$SemModelo,
    [switch]$SemDados,
    [switch]$SemVerificarGPU,
    [switch]$SomenteVerificar,
    [switch]$ComTestes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "Este instalador exige PowerShell de 64 bits no Windows x64."
}
$projectRoot = $PSScriptRoot
$environmentPath = if ([IO.Path]::IsPathRooted($Ambiente)) { [IO.Path]::GetFullPath($Ambiente) } else { [IO.Path]::GetFullPath((Join-Path $projectRoot $Ambiente)) }
$pythonPath = Join-Path $environmentPath "Scripts\python.exe"

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "O comando '$Program' falhou (código $LASTEXITCODE)." }
}

Push-Location -LiteralPath $projectRoot
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    if (-not $SomenteVerificar) {
        if (-not (Test-Path -LiteralPath "$env:WINDIR\System32\msvcp140.dll")) {
            $runtimePath = Join-Path $projectRoot ".tools\vc_redist.x64.exe"
            New-Item -ItemType Directory -Path (Split-Path -Parent $runtimePath) -Force | Out-Null
            Write-Host "Instalando o runtime oficial do Visual C++ (pode solicitar permissão de administrador)..."
            Invoke-WebRequest -UseBasicParsing -Uri "https://aka.ms/vc14/vc_redist.x64.exe" -OutFile $runtimePath
            $runtime = Start-Process -FilePath $runtimePath -ArgumentList "/install", "/quiet", "/norestart" -Verb RunAs -WindowStyle Hidden -Wait -PassThru
            if ($runtime.ExitCode -eq 3010) { throw "O runtime foi instalado. Reinicie o Windows e execute o script novamente." }
            if ($runtime.ExitCode -notin @(0, 1638)) { throw "A instalação do Visual C++ falhou (código $($runtime.ExitCode))." }
        } else {
            Write-Host "[Reutilizar] Runtime do Visual C++ já encontrado; nenhuma instalação necessária."
        }
        $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
        $localUv = Join-Path $projectRoot ".tools\uv\uv.exe"
        if ($uvCommand) {
            $uvPath = $uvCommand.Source
            Write-Host "[Reutilizar] uv já encontrado no PATH: $uvPath"
        }
        elseif (Test-Path -LiteralPath $localUv) {
            $uvPath = $localUv
            Write-Host "[Reutilizar] uv já instalado neste projeto: $uvPath"
        }
        else {
            Write-Host "Instalando uv na pasta local .tools (sem alterar o PATH do sistema)..."
            $oldInstallDir = $env:UV_INSTALL_DIR
            $oldModifyPath = $env:UV_NO_MODIFY_PATH
            try {
                $env:UV_INSTALL_DIR = Split-Path -Parent $localUv
                $env:UV_NO_MODIFY_PATH = "1"
                Invoke-RestMethod "https://astral.sh/uv/0.12.3/install.ps1" | Invoke-Expression
            } finally {
                $env:UV_INSTALL_DIR = $oldInstallDir
                $env:UV_NO_MODIFY_PATH = $oldModifyPath
            }
            if (-not (Test-Path -LiteralPath $localUv)) { throw "O uv não foi instalado. Confira a conexão e execute novamente." }
            $uvPath = $localUv
        }
        $env:UV_PROJECT_ENVIRONMENT = $environmentPath
        Invoke-Checked $uvPath @("--version")
        if (Test-Path -LiteralPath $pythonPath) {
            Write-Host "[Conferir] Ambiente existente em $environmentPath; pacotes compatíveis serão mantidos."
        } else {
            $previousErrorAction = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                $existingPython = & $uvPath python find 3.12 --system --no-project --no-python-downloads 2>$null
                $pythonFound = $LASTEXITCODE -eq 0
            } finally {
                $ErrorActionPreference = $previousErrorAction
            }
            if ($pythonFound) { Write-Host "[Reutilizar] Python 3.12 já disponível: $existingPython" }
            else { Write-Host "[Baixar] Python 3.12 ainda não encontrado; o uv vai prepará-lo automaticamente." }
            Write-Host "[Criar] Ambiente isolado em $environmentPath."
        }
        Write-Host "[Conferir] Sincronizando uv.lock: instalar somente o que faltar ou estiver em versão diferente, sem remover pacotes extras."
        Invoke-Checked $uvPath @("sync", "--locked", "--inexact", "--python", "3.12")
        $prepareArgs = @("-m", "tools.preparar_ambiente")
        if ($SemModelo) { $prepareArgs += "--sem-modelo" }
        if ($SemDados) { $prepareArgs += "--sem-dados" }
        Invoke-Checked $pythonPath $prepareArgs
    }
    Write-Host "[Verificar] Conferindo dependências, GPU e arquivos solicitados..."
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Ambiente ausente em $environmentPath. Execute sem -SomenteVerificar para instalar." }
    $verifyArgs = @("-m", "tools.verificar_ambiente")
    if ($SemModelo) { $verifyArgs += "--sem-modelo" }
    if ($SemDados) { $verifyArgs += "--sem-dados" }
    if (-not $SemVerificarGPU) { $verifyArgs += "--exigir-gpu" }
    Invoke-Checked $pythonPath $verifyArgs
    if ($ComTestes) {
        Invoke-Checked $pythonPath @("-m", "unittest", "benchmark.test_scoring")
        Invoke-Checked $pythonPath @("-m", "unittest", "discover", "-s", "dashboard", "-p", "test_*.py")
        Invoke-Checked $pythonPath @("-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py")
    }
    Write-Host "Configuração concluída. Abra .\abrir_painel.bat (ambiente padrão) ou execute $pythonPath -m dashboard.server --open."
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
    Pop-Location
}
