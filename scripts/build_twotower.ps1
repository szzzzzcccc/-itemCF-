param(
    [string]$BackendService = "backend",
    [string]$DbService = "postgres",
    [int]$Epochs = 2,
    [int]$NegPerPos = 2,
    [int]$MaxUsers = 20000,
    [int]$MinUserPositives = 5
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Output "[step] ensuring postgres and backend are running..."
docker compose up -d $DbService $BackendService | Out-Null

Write-Output "[step] training two-tower embeddings..."
docker compose exec -T `
  -e TWOTOWER_EPOCHS=$Epochs `
  -e TWOTOWER_NEG_PER_POS=$NegPerPos `
  -e TWOTOWER_MAX_USERS=$MaxUsers `
  -e TWOTOWER_MIN_USER_POSITIVES=$MinUserPositives `
  $BackendService python app/build_twotower.py
