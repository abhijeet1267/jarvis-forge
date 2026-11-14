# Vantage Windows Installer
# Run this in PowerShell as Administrator for full setup:
#   powershell -ExecutionPolicy Bypass -File installers\windows\install.ps1
param(
    [switch]$UserOnly,        # Install for current user only (no admin needed)
    [switch]$SkipPython,      # Skip Python check (if you already have Python 3.10+)
    [string]$InstallPath = "$env:LOCALAPPDATA\Vantage"
)

$ErrorActionPreference = "Stop"
$VERSION = "0.2.0"
$APP_NAME = "Vantage"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  $APP_NAME v$VERSION Windows Installer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Python check ───────────────────────────────────────────
if (-not $SkipPython) {
    Write-Host "[1/5] Checking Python..." -ForegroundColor Yellow
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        $python = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $python) {
        Write-Host ""
        Write-Host "Python 3.10+ is required but not found." -ForegroundColor Red
        Write-Host "Install it from: https://www.python.org/downloads/" -ForegroundColor White
        Write-Host "Make sure to check 'Add Python to PATH' during installation." -ForegroundColor White
        Write-Host ""
        Write-Host "Then re-run this script, or use: .\install.ps1 -SkipPython" -ForegroundColor White
        exit 1
    }
    $pyVersion = & $python.Source --version 2>&1
    Write-Host "  Found: $pyVersion at $($python.Source)" -ForegroundColor Green
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
}

# ── Step 2: Install directory ──────────────────────────────────────
Write-Host "[2/5] Setting up install directory..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null
Write-Host "  Installing to: $InstallPath" -ForegroundColor Green

# ── Step 3: Copy application files ─────────────────────────────────
Write-Host "[3/5] Copying application files..." -ForegroundColor Yellow

# The source directory is where this script lives (installers/windows/..)
$SourceDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

# Copy core application
$dirs = @("jarvis", "dashboard")
foreach ($dir in $dirs) {
    $src = Join-Path $SourceDir $dir
    $dst = Join-Path $InstallPath $dir
    if (Test-Path $src) {
        if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
        Copy-Item -Recurse $src $dst
        Write-Host "  Copied: $dir" -ForegroundColor Green
    }
}

# Copy individual files
$files = @("run.py", "requirements.txt", "pyproject.toml", ".env.example")
foreach ($file in $files) {
    $src = Join-Path $SourceDir $file
    $dst = Join-Path $InstallPath $file
    if (Test-Path $src) {
        Copy-Item $src $dst
    }
}

# Create .env from example if it doesn't exist
$envFile = Join-Path $InstallPath ".env"
if (-not (Test-Path $envFile)) {
    $envExample = Join-Path $InstallPath ".env.example"
    if (Test-Path $envExample) {
        Copy-Item $envExample $envFile
        Write-Host "  Created .env from .env.example" -ForegroundColor Green
    }
}

# ── Step 4: Install Python dependencies ────────────────────────────
Write-Host "[4/5] Installing Python dependencies..." -ForegroundColor Yellow
Push-Location $InstallPath
& $python.Source -m pip install -r requirements.txt --quiet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Warning: pip install had errors. Some features may not work." -ForegroundColor Yellow
} else {
    Write-Host "  Dependencies installed." -ForegroundColor Green
}
Pop-Location

# ── Step 5: Create shortcuts ───────────────────────────────────────
Write-Host "[5/5] Creating shortcuts..." -ForegroundColor Yellow

# Launcher batch script
$launcher = @"
@echo off
title $APP_NAME
cd /d "$InstallPath"
"$($python.Source)" run.py serve
pause
"@
$launcherPath = Join-Path $InstallPath "start-vantage.bat"
$launcher | Out-File -Encoding ASCII $launcherPath
Write-Host "  Created: start-vantage.bat" -ForegroundColor Green

# Desktop shortcut
$WshShell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcut = $WshShell.CreateShortcut("$desktop\$APP_NAME.lnk")
$shortcut.TargetPath = "cmd.exe"
$shortcut.Arguments = "/c `"$launcherPath`""
$shortcut.WorkingDirectory = $InstallPath
$shortcut.IconLocation = "$InstallPath\dashboard\icon-512.png,0"
$shortcut.Description = "$APP_NAME - Your AI Assistant"
$shortcut.Save()
Write-Host "  Created: Desktop shortcut" -ForegroundColor Green

# Start Menu shortcut
$startMenu = [Environment]::GetFolderPath("StartMenu") + "\Programs\$APP_NAME"
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
$smShortcut = $WshShell.CreateShortcut("$startMenu\$APP_NAME.lnk")
$smShortcut.TargetPath = "cmd.exe"
$smShortcut.Arguments = "/c `"$launcherPath`""
$smShortcut.WorkingDirectory = $InstallPath
$smShortcut.Description = "$APP_NAME - Your AI Assistant"
$smShortcut.Save()
Write-Host "  Created: Start Menu shortcut" -ForegroundColor Green

# ── Done ───────────────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Installation Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Installed to: $InstallPath" -ForegroundColor White
Write-Host "Launch from:  Desktop shortcut, or run:" -ForegroundColor White
Write-Host "  $launcherPath" -ForegroundColor White
Write-Host ""
Write-Host "After starting Vantage, open:" -ForegroundColor White
Write-Host "  http://localhost:8765        (dashboard)" -ForegroundColor White
Write-Host "  http://localhost:8765/phone  (phone UI)" -ForegroundColor White
Write-Host ""
Write-Host "For Android access, use: http://<this-pc-ip>:8765/phone" -ForegroundColor White