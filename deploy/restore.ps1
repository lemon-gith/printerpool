# Restore a backup made by backup.ps1 or the worker. This REPLACES the current database (and uploaded files, if the
# backup has them). The app and worker are stopped while it runs, then started again.
#
#   .\deploy\restore.ps1 backups\printpool-20261029-033000.zip
#   .\deploy\restore.ps1 <zip> --no-files    restore only the database
#
# The password comes from BACKUP_PASSWORD in .env. For a backup made with a different password, set
# $env:BACKUP_PASSWORD for this run (or leave it empty in .env to be asked).
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
$ErrorActionPreference = "Stop"
$zipPath = (Resolve-Path $Zip).Path
Set-Location (Split-Path -Parent $PSScriptRoot)
$folder = Split-Path -Parent $zipPath
$name = Split-Path -Leaf $zipPath

$answer = Read-Host "This replaces everything in the PrintPool database with $name.`nType `"restore`" to continue"
if ($answer -ne "restore") { Write-Host "Cancelled."; exit 1 }

$passwordEnv = @()
if ($env:BACKUP_PASSWORD) { $passwordEnv = @("-e", "BACKUP_PASSWORD") }

docker compose up -d db
docker compose stop app worker
docker compose run --rm --no-deps @passwordEnv -v "${folder}:/restore:ro" worker python manage.py restore_backup "/restore/$name" --yes @Rest
$status = $LASTEXITCODE
docker compose start app worker
exit $status
