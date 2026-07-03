<#
  apply-migrations.ps1 - apply every *.sql in this folder (filename order) to the
  local docker Postgres. Idempotent as long as the SQL uses IF NOT EXISTS.

  This is the Option-A "migration runner": we don't use Alembic yet, so schema
  changes are plain numbered SQL files applied by this script against the RUNNING
  database (not the init/ dir, which only runs once on a fresh volume).

  Usage:  .\apply-migrations.ps1        (run after `docker compose up` / dev.ps1)
#>

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$container = "prodrag-postgres"

$files = Get-ChildItem "$dir\*.sql" | Sort-Object Name
if (-not $files) { Write-Host "No .sql migrations found in $dir"; exit 0 }

foreach ($f in $files) {
    Write-Host "Applying $($f.Name)..."
    # Pipe the SQL into psql inside the container. ON_ERROR_STOP makes psql exit
    # non-zero on the first error so a bad migration fails loudly.
    Get-Content $f.FullName -Raw | docker exec -i $container psql -U prodrag -d prodrag -v ON_ERROR_STOP=1 -q
    if ($LASTEXITCODE -ne 0) { throw "Migration $($f.Name) failed (exit $LASTEXITCODE)" }
}
Write-Host "All migrations applied."
