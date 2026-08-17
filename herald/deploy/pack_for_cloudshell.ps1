# Zip the HARALD source for OCI Cloud Shell upload.
# Excludes wallet, .env, and local junk. Run from anywhere:
#   powershell -File herald\deploy\pack_for_cloudshell.ps1

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$out  = Join-Path ([Environment]::GetFolderPath("Desktop")) "harald-src.zip"

if (Test-Path $out) { Remove-Item $out -Force }

$exclude = @(
  '\\wallet\\', '\\.env$', '\\.venv\\', '\\__pycache__\\',
  '\\.pytest_cache\\', 'bucket_listing\.json$', '\\.git\\'
)

$files = Get-ChildItem -Path $root -Recurse -File | Where-Object {
  $p = $_.FullName
  -not ($exclude | Where-Object { $p -match $_ })
}

Compress-Archive -Path ($files.FullName) -DestinationPath $out -Force
# Compress-Archive with FullName list flattens paths — rebuild properly:
Remove-Item $out -Force

Push-Location $root
$temp = Join-Path $env:TEMP "harald-pack"
if (Test-Path $temp) { Remove-Item $temp -Recurse -Force }
New-Item -ItemType Directory -Path $temp | Out-Null
robocopy $root $temp /E /NFL /NDL /NJH /NJS /nc /ns /np `
  /XD wallet .venv __pycache__ .pytest_cache .git `
  /XF .env bucket_listing.json *.pyc | Out-Null
Compress-Archive -Path (Join-Path $temp '*') -DestinationPath $out -Force
Remove-Item $temp -Recurse -Force
Pop-Location

Write-Host "Created $out"
Write-Host "Upload that file in Cloud Shell, then follow deploy\DEPLOY.md"
