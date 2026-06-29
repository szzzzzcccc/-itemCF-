param(
    [string]$DbService = "postgres",
    [string]$AirflowService = "airflow"
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Output "[step] ensuring postgres and airflow are running..."
docker compose up -d $DbService $AirflowService | Out-Null

Write-Output "[done] airflow ui: http://localhost:8081"
Write-Output "[tip] first startup may take a few minutes while airflow initializes."
