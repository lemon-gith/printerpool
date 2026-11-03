# Deploy or update PrintPool on Windows (Docker Desktop). Everything runs in Docker: this changes nothing on the
# machine outside this folder.
#
#   Copy-Item printpool.example.toml printpool.toml   # first time: then edit it
#   .\deploy\setup-server.ps1                         # first deploy, after editing printpool.toml, and after git pull
#
# Optional, first deploy only: set $env:DJANGO_SUPERUSER_USERNAME and $env:DJANGO_SUPERUSER_PASSWORD to create the
# first admin (who must change that password at first login).
# If PowerShell refuses to run scripts:  powershell -ExecutionPolicy Bypass -File .\deploy\setup-server.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Say($text) { Write-Host "`n==> $text" -ForegroundColor White }
function Fail($text) { Write-Host "error: $text" -ForegroundColor Red; exit 1 }

docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop (with docker compose) is needed." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "can't talk to Docker. Is Docker Desktop running?" }
if (-not (Test-Path printpool.toml)) { Fail "printpool.toml not found. Copy printpool.example.toml to printpool.toml and edit it first." }

Say "Checking printpool.toml"
# Rendered in a throwaway container, so the only thing this machine needs is Docker.
docker run --rm -v "$((Get-Location).Path):/work" -w /work python:3.13-slim python -I deploy/render_config.py
if ($LASTEXITCODE -ne 0) { Fail "fix printpool.toml (or .env) and run this again." }

Say "Building and starting"
docker compose up -d --build --remove-orphans
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed (see above)." }

Say "Waiting for everything to be healthy"
foreach ($service in "app", "worker", "gateway") {
    $status = ""
    for ($i = 0; $i -lt 90; $i++) {
        $id = docker compose ps -q $service
        $status = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $id 2>$null
        if ($status -eq "healthy") { break }
        Start-Sleep -Seconds 2
    }
    if ($status -eq "healthy") {
        Write-Host ("  {0,-8} healthy" -f $service)
    } else {
        Write-Host ("  {0,-8} {1}" -f $service, $status)
        docker compose logs --tail 30 $service
        Fail "$service didn't become healthy (logs above)."
    }
}

if ($env:DJANGO_SUPERUSER_USERNAME -and $env:DJANGO_SUPERUSER_PASSWORD) {
    Say "Creating the first admin account"
    docker compose exec -T -e DJANGO_SUPERUSER_USERNAME -e DJANGO_SUPERUSER_PASSWORD app python manage.py create_initial_admin
}

Say "Running"
docker compose ps --format '  {{.Service}}: {{.Status}}'
Write-Host @"

Next:
  - First time? Create your admin:  docker compose exec app python manage.py createsuperuser
  - Logs:                           docker compose logs -f
  - Back up now / restore:          .\deploy\backup.ps1   /   .\deploy\restore.ps1 <backup.zip>
  - After git pull or editing printpool.toml, run .\deploy\setup-server.ps1 again.
"@
