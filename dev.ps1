# Doblarr local dev helper (Windows / PowerShell)
#
#   .\dev.ps1 setup          install Python + Node deps
#   .\dev.ps1 serve          run the web UI + API (http://127.0.0.1:6363)
#   .\dev.ps1 test           Python tests (pytest)
#   .\dev.ps1 test-web       frontend unit tests (node --test)
#   .\dev.ps1 test-browser   Playwright browser tests
#   .\dev.ps1 lint           ruff + mypy + eslint
#   .\dev.ps1 check          everything CI runs (minus docker build)
#   .\dev.ps1 cli <args...>  pass through to the doblarr CLI
#
# Examples:
#   .\dev.ps1 cli check
#   .\dev.ps1 cli dub movie.mkv --to es --dry-run

param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Prefer the project venv if it exists, else fall back to system python.
$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

function Invoke-Step([string]$Name, [scriptblock]$Block) {
    Write-Host "`n==> $Name" -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

switch ($Command) {
    "setup" {
        Invoke-Step "pip install -e .[dev]" { & $Python -m pip install -e ".[dev]" }
        Invoke-Step "npm ci" { npm ci }
        Invoke-Step "playwright install chromium" { npx playwright install chromium }
    }
    "serve" {
        & $Python -m doblarr serve @Rest
    }
    "test" {
        & $Python -m pytest -q @Rest
        exit $LASTEXITCODE
    }
    "test-web" {
        npm test
        exit $LASTEXITCODE
    }
    "test-browser" {
        npx playwright test @Rest
        exit $LASTEXITCODE
    }
    "lint" {
        Invoke-Step "ruff" { & $Python -m ruff check . }
        Invoke-Step "mypy" { & $Python -m mypy doblarr/ }
        Invoke-Step "eslint" { npm run lint }
    }
    "check" {
        Invoke-Step "ruff" { & $Python -m ruff check . }
        Invoke-Step "mypy" { & $Python -m mypy doblarr/ }
        Invoke-Step "pytest" { & $Python -m pytest -q --cov=doblarr --cov-report=term-missing --cov-fail-under=70 }
        Invoke-Step "npm run check" { npm run check }
        Write-Host "`nAll checks passed." -ForegroundColor Green
    }
    "cli" {
        & $Python -m doblarr @Rest
        exit $LASTEXITCODE
    }
    default {
        Get-Content $PSCommandPath -TotalCount 14
    }
}
