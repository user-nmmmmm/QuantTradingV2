param(
    [int]$Port = 8765,
    [switch]$Demo,
    [switch]$NoBacktests,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$projectPython = Join-Path $projectDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    $projectPython = (Get-Command python -ErrorAction Stop).Source
}
$dashboardArguments = @('-m', 'dashboard.web', '--port', "$Port")
if ($Demo) { $dashboardArguments += '--demo' }
if ($NoBacktests) { $dashboardArguments += '--no-backtests' }
if (-not $NoBrowser) { $dashboardArguments += '--open' }
Push-Location -LiteralPath $projectDirectory
try {
    & $projectPython @dashboardArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
