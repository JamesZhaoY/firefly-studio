# fly-deploy.ps1 — run AFTER `fly auth login` succeeds
# Usage:
#   cd D:\workspace\app\adobe
#   fly auth login                                  # one-time, opens browser
#   .\fly-deploy.ps1

$ErrorActionPreference = 'Stop'
$fly = "C:\Users\17664\.fly\bin\flyctl.exe"
$root = "D:\workspace\app\adobe"

# Verify auth
$auth = & $fly auth whoami 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Host "Not logged in. Run:  $fly auth login" -ForegroundColor Yellow
  exit 1
}
Write-Host "Auth OK: $auth"

# Verify credentials exist
$storage = Join-Path $root 'data\storage.json'
$token   = Join-Path $root 'data\current_token.json'
foreach ($f in @($storage, $token)) {
  if (-not (Test-Path $f)) {
    Write-Host "Missing: $f" -ForegroundColor Red
    Write-Host "Run: cd $root; python token_daemon.py --start"
    exit 1
  }
}

# App name (must match fly.toml: app: firefly-studio)
$app = "firefly-studio"
$region = "nrt"   # Tokyo

# Create app if missing
$apps = & $fly apps list --json 2>&1 | ConvertFrom-Json
if (-not ($apps | Where-Object { $_.Name -eq $app })) {
  Write-Host "Creating app $app ..."
  & $fly apps create $app --org personal 2>&1 | Select-Object -First 5
}

# Create persistent volume (1GB, free tier allows up to 3GB)
Write-Host "Creating volume firefly_data (1GB @ $region)..."
$volumes = & $fly volumes list --json 2>&1 | ConvertFrom-Json
if (-not ($volumes | Where-Object { $_.Name -eq 'firefly_data' })) {
  & $fly volumes create firefly_data --size 1 --region $region 2>&1 | Select-Object -First 5
}

# Upload Adobe credentials as secrets
Write-Host "Uploading STORAGE_JSON / TOKEN_JSON secrets..."
$storageBody = Get-Content $storage -Raw
$tokenBody   = Get-Content $token   -Raw
& $fly secrets set --stage STORAGE_JSON="$storageBody" 2>&1 | Select-Object -Last 3
& $fly secrets set --stage TOKEN_JSON="$tokenBody"   2>&1 | Select-Object -Last 3

# Deploy (builds the Dockerfile and ships it)
Write-Host "Deploying..."
& $fly deploy --remote-only 2>&1 | Select-Object -Last 20

Write-Host ""
Write-Host "Done. Verify:"
Write-Host "  curl https://$app.fly.dev/api/health"
Write-Host ""
Write-Host "Set GitHub Pages env VITE_API_BASE to: https://$app.fly.dev"