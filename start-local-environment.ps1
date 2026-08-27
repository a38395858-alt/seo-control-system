$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DatabasePath = Join-Path $ProjectRoot "data\seo-control.sqlite3"
$LogDirectory = Join-Path $ProjectRoot "data"
$StandardLog = Join-Path $LogDirectory "local-environment.stdout.log"
$ErrorLog = Join-Path $LogDirectory "local-environment.stderr.log"

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null

if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    exit 0
}

$env:PYTHONPATH = (Join-Path $ProjectRoot "src") + [IO.Path]::PathSeparator + $env:PYTHONPATH
$env:SEO_RUNTIME_DATABASE_MODE = "sqlite"
$env:SEO_TASK_QUEUE_BACKEND = "local"
$env:SEO_LOCAL_WORKER_CONCURRENCY = "2"

Set-Location -LiteralPath $ProjectRoot

$arguments = @(
    "-m", "seo_control", "serve",
    "--host", "127.0.0.1",
    "--port", "8000",
    "--database", $DatabasePath,
    "--database-mode", "sqlite"
)

$process = Start-Process `
    -FilePath "python.exe" `
    -ArgumentList $arguments `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StandardLog `
    -RedirectStandardError $ErrorLog `
    -PassThru `
    -Wait

if ($process.ExitCode -ne 0) {
    "[$(Get-Date -Format o)] SEO Control exited with code $($process.ExitCode)." | Add-Content -LiteralPath $ErrorLog -Encoding UTF8
    exit $process.ExitCode
}
