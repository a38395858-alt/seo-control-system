$ErrorActionPreference = "Stop"

$response = Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/api/runtime-database/rollback" `
    -ContentType "application/json" `
    -Body '{"confirmation":"ROLLBACK_TO_SQLITE"}'

if ($response.mode -ne "sqlite") {
    throw "Runtime rollback did not return SQLite mode."
}
Write-Host "Workspace runtime source is now SQLite. PostgreSQL data was retained."
