# Loads .env (if present) into the process environment, then runs the service.
# Usage: ./run.ps1   (from this service directory)
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        Set-Item "env:$($k.Trim())" $v.Trim()
    }
    Write-Host "Loaded .env" -ForegroundColor Green
}
else {
    Write-Host "No .env found - running with current environment" -ForegroundColor Yellow
}
& (Join-Path $PSScriptRoot "mvnw.cmd") spring-boot:run
