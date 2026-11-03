# Make a backup now: a zip of the database (and uploaded files, if configured), AES-256 encrypted if configured.
# Uses [backups] in printpool.toml for everything you don't override here.
#
#   .\deploy\backup.ps1                          into the [backups.location] from printpool.toml
#   .\deploy\backup.ps1 -To D:\PrintPoolBackups  into another folder on this machine
#   other options are passed through: --keep N  --include-files | --no-include-files  --encrypt | --no-encrypt
#
# Needs PrintPool running (.\deploy\setup-server.ps1). To run it on a schedule from Task Scheduler, see
# deploy\SCHEDULING.md, or let the worker do it ([backups] enabled = true in printpool.toml).
param(
    [string]$To = "",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($To -eq "") {
    # The worker already has the configured backup location mounted at /backups.
    docker compose exec -T worker python manage.py backup @Rest
} else {
    New-Item -ItemType Directory -Force -Path $To | Out-Null
    $folder = (Resolve-Path $To).Path
    docker compose run --rm --no-deps -T -v "${folder}:/backup-out" worker python manage.py backup --output /backup-out @Rest
}
exit $LASTEXITCODE
