# Windows PowerShell launcher: .\workshop.ps1 <command> [options]
# Arguments are forwarded unchanged (no param block, so --flags are not parsed by PowerShell).
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'workshop.py'

$candidates = @()
if ($env:WORKSHOP_PYTHON) { $candidates += , @($env:WORKSHOP_PYTHON) }
$candidates += , @('py', '-3')
$candidates += , @('python')
$candidates += , @('python3')

foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $prefix = @($candidate | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    & $exe @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    & $exe @prefix $script @args
    exit $LASTEXITCODE
}

Write-Error 'Python 3.8 or newer is required. Install it from https://www.python.org/downloads/ or set WORKSHOP_PYTHON.'
exit 3
