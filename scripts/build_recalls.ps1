param(
    [string]$BackendService = "backend",
    [string]$DbService = "postgres"
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Output "[step] ensuring postgres and backend are running..."
docker compose up -d $DbService $BackendService | Out-Null

Write-Output "[step] building popular recall and sparse itemcf recall..."
docker compose exec -T $BackendService python app/build_recalls_sparse.py
