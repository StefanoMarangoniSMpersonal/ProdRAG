<#
  dev.ps1 - one-shot local dev environment for ProdRAG.

  Idempotent: the FIRST run also does first-time setup (Python venv, backend
  deps, frontend deps, .env files) only where those are missing; EVERY run then
  starts every piece of the stack. Safe to run repeatedly.

    Infra (docker):   Postgres + pgvector, Redis   -> runs in the background
    Backend:          FastAPI (uvicorn --reload)   -> opens its own window
    Worker:           Celery worker                -> opens its own window
    Beat (reaper):    Celery beat (stuck-job scan) -> opens its own window
    Frontend:         Next.js (npm run dev)        -> opens its own window

  Windows note: Beat runs as a SEPARATE process, never embedded in the worker via
  '-B' -- Celery rejects '-B' on Windows ("does not work on Windows"). On Linux
  '-B' would fuse them; here they must be two processes.

  Usage:
    .\dev.ps1              # setup-if-needed, then start everything
    .\dev.ps1 -Reinstall   # force-reinstall backend + frontend deps first
    .\stop.ps1             # stop the docker containers (close the server windows to stop them)
#>

param(
    [switch]$Reinstall  # force pip/npm install even if deps already present
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Require-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "Required tool '$name' is not on your PATH. Install it and re-run."
    }
}

# --- 0. Prerequisites -------------------------------------------------------
Write-Step "Checking prerequisites (docker, python, node, npm)"
Require-Tool docker
Require-Tool python
Require-Tool node
Require-Tool npm
Write-Host "All prerequisites found."

# --- 1. Infra: Postgres + Redis via docker-compose --------------------------
Write-Step "Starting infra (Postgres + pgvector, Redis)"
Push-Location "$root\infra"
try {
    docker compose up -d
    # Wait until Postgres reports healthy (its healthcheck runs pg_isready).
    # Without this the backend can race the DB on a cold start and 500 once.
    Write-Host "Waiting for Postgres to become healthy..." -NoNewline
    for ($i = 0; $i -lt 30; $i++) {
        $health = (docker inspect --format '{{.State.Health.Status}}' prodrag-postgres 2>$null)
        if ($health -eq "healthy") { Write-Host " healthy."; break }
        Start-Sleep -Seconds 1
        Write-Host "." -NoNewline
    }
    if ($health -ne "healthy") { Write-Host " (still '$health' - continuing anyway)" }
}
finally { Pop-Location }

# --- 1b. Apply DB migrations (idempotent) -----------------------------------
# Schema lives in infra/db/migrations/ (Option A: plain numbered SQL, no Alembic
# yet). Safe to run every start because the migrations use IF NOT EXISTS.
Write-Step "Applying database migrations"
& "$root\infra\db\migrations\apply-migrations.ps1"

# --- 2. Backend setup (only what's missing) ---------------------------------
Write-Step "Preparing backend (venv, deps, .env)"
Push-Location "$root\backend"
try {
    $freshVenv = $false
    if (-not (Test-Path ".venv")) {
        Write-Host "Creating Python virtual environment (.venv)..."
        python -m venv .venv
        $freshVenv = $true
    }
    $py = ".\.venv\Scripts\python.exe"
    if ($freshVenv -or $Reinstall) {
        Write-Host "Installing backend dependencies..."
        & $py -m pip install --upgrade pip -q
        & $py -m pip install -q -r requirements.txt -r requirements-dev.txt
    } else {
        Write-Host "Backend deps present (use -Reinstall to force)."
    }
    if (-not (Test-Path ".env")) {
        Write-Host "Creating backend .env from .env.example..."
        Copy-Item ".env.example" ".env"
    }
}
finally { Pop-Location }

# --- 3. Frontend setup (only what's missing) --------------------------------
Write-Step "Preparing frontend (node_modules, .env.local)"
Push-Location "$root\frontend"
try {
    if ($Reinstall -or -not (Test-Path "node_modules")) {
        Write-Host "Installing frontend dependencies (npm install)..."
        npm install
    } else {
        Write-Host "Frontend deps present (use -Reinstall to force)."
    }
    if (-not (Test-Path ".env.local")) {
        Write-Host "Creating frontend .env.local from .env.local.example..."
        Copy-Item ".env.local.example" ".env.local"
    }
}
finally { Pop-Location }

# --- 4. Launch the dev servers, each in its own window ----------------------
# Start-Process opens a fresh PowerShell window per server so you get live,
# separately-scrollable logs and can Ctrl-C each one independently. -NoExit
# keeps the window open after the server stops so you can read any error.
Write-Step "Launching backend + worker + beat + frontend (each in its own window)"

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\backend'; .\.venv\Scripts\Activate.ps1; " +
    "Write-Host 'ProdRAG API - http://localhost:8000' -ForegroundColor Green; " +
    "uvicorn app.main:app --reload"
)

# The Celery worker consumes ingestion jobs from Redis. --pool=solo because Celery's
# default prefork pool is broken on Windows. Runs from backend/ so it loads the same
# .env (GEMINI_API_KEY) the app does. NOTE: no -B here -- Beat is a separate window
# below, because embedded Beat (-B) is unsupported on Windows.
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\backend'; .\.venv\Scripts\Activate.ps1; " +
    "Write-Host 'ProdRAG Worker - Celery worker' -ForegroundColor Green; " +
    "celery -A app.worker worker -l info --pool=solo"
)

# Celery Beat: the scheduler that periodically enqueues the stuck-job reaper
# (reap_stuck_documents). Must be its own process on Windows (see -B note above);
# the worker above executes whatever Beat schedules onto the queue.
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\backend'; .\.venv\Scripts\Activate.ps1; " +
    "Write-Host 'ProdRAG Beat - Celery beat (reaper scheduler)' -ForegroundColor Green; " +
    "celery -A app.worker beat -l info"
)

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\frontend'; " +
    "Write-Host 'ProdRAG Frontend - http://localhost:3000' -ForegroundColor Green; " +
    "npm run dev"
)

Write-Step "Up."
Write-Host "  API:      http://localhost:8000/health/db"
Write-Host "  Frontend: http://localhost:3000"
Write-Host "`nFour new windows are running the API, worker, beat, and frontend. To stop:"
Write-Host "  - Ctrl-C in each server window (or just close them)"
Write-Host "  - .\stop.ps1   to stop the Postgres/Redis containers"
