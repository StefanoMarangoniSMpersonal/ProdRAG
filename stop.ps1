<#
  stop.ps1 - stop the ProdRAG local infra.

  This stops the Postgres + Redis docker containers. The two dev servers run in
  their own windows (opened by dev.ps1) - close those windows or Ctrl-C them to
  stop the API and frontend.

  Usage:
    .\stop.ps1        # docker compose down (keeps the pgdata volume / your data)
    .\stop.ps1 -Wipe   # docker compose down -v (ALSO deletes the Postgres volume)
#>

param(
    [switch]$Wipe  # also remove the pgdata named volume (destroys all DB data)
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Push-Location "$root\infra"
try {
    if ($Wipe) {
        Write-Host "Stopping containers AND deleting the pgdata volume..." -ForegroundColor Yellow
        docker compose down -v
    } else {
        Write-Host "Stopping containers (Postgres data is preserved)..."
        docker compose down
    }
}
finally { Pop-Location }

Write-Host "Infra stopped. Server windows (if open) must be closed separately."
