#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'),
    [string]$DistPath = 'apps',
    [string]$WorkPath = 'build'
)
$ErrorActionPreference = 'Stop'
$previousPath = $env:PATH
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $Python = (Resolve-Path -LiteralPath $Python).Path
    $pythonBase = (& $Python -c 'import sys; print(sys.base_prefix)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the Python runtime' }
    # Resolve DLLs from this interpreter, never from unrelated tools on PATH.
    $env:PATH = @((Split-Path $Python), $pythonBase,
        (Join-Path $pythonBase 'Library\bin'), (Join-Path $pythonBase 'DLLs'),
        (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $Python -m PyInstaller --noconfirm --clean --distpath $DistPath --workpath $WorkPath config/Gauge.spec
    if ($LASTEXITCODE -ne 0) { throw "Build failed with exit code $LASTEXITCODE" }
} finally {
    $env:PATH = $previousPath
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
    Pop-Location
}
