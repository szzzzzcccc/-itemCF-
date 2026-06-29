param(
    [string]$BackendService = "backend",
    [string]$DbService = "postgres",
    [int]$MaxUsers = 3000,
    [int]$MinPositives = 5
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Output "[step] ensuring postgres and backend are running..."
docker compose up -d $DbService $BackendService | Out-Null

Write-Output "[step] training LightGBM ranker..."
docker compose exec -T `
  -e LGB_MAX_USERS=$MaxUsers `
  -e LGB_MIN_POSITIVES=$MinPositives `
  $BackendService python app/build_lgb_ranker.py
