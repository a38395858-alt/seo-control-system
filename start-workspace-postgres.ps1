$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DatabasePath = Join-Path $ProjectRoot "data\seo-control.sqlite3"
$StatePath = Join-Path $ProjectRoot "data\runtime-database-state.json"
$TargetDatabase = "seo_agent_p68_runtime_20260803"

$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
if ($state.mode -ne "postgres" -or $state.last_gate.ready -ne $true) {
    throw "PostgreSQL cutover has not passed the persisted runtime gate."
}

if (-not $env:DATABASE_URL) {
    $container = (docker inspect seo-agent-platform-postgres-1 | ConvertFrom-Json)[0]
    $containerEnvironment = @{}
    foreach ($entry in $container.Config.Env) {
        $parts = $entry -split "=", 2
        $containerEnvironment[$parts[0]] = $parts[1]
    }
    $databaseUser = [uri]::EscapeDataString($containerEnvironment["POSTGRES_USER"])
    $databasePassword = [uri]::EscapeDataString($containerEnvironment["POSTGRES_PASSWORD"])
    $env:DATABASE_URL = "postgresql://${databaseUser}:${databasePassword}@127.0.0.1:5432/${TargetDatabase}"
}

$parsedDatabase = [uri]$env:DATABASE_URL
if ($parsedDatabase.AbsolutePath.TrimStart("/") -ne $TargetDatabase) {
    throw "DATABASE_URL must target the isolated $TargetDatabase database."
}
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw "Port 8000 is already in use. Stop the existing workspace before starting another instance."
}

$env:PYTHONPATH = (Join-Path $ProjectRoot "src") + [IO.Path]::PathSeparator + $env:PYTHONPATH
$env:SEO_RUNTIME_DATABASE_MODE = "postgres"
Set-Location -LiteralPath $ProjectRoot
& python -m seo_control serve --host 127.0.0.1 --port 8000 --database $DatabasePath --database-mode postgres --runtime-state $StatePath
