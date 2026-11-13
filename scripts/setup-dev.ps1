<#
.SYNOPSIS
  Ropi development environment bootstrap (Windows 10/11, PowerShell 5.1+).

.DESCRIPTION
  Installs: Git, VS Code, Python 3.12, FFmpeg, GitHub CLI (via winget or portable fallback),
  VS Code extensions, clones the repo (or uses current repo), creates .venv and installs requirements.
  Safe to run multiple times (idempotent).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup-dev.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\setup-dev.ps1 -WithAI
#>
param(
    [string]$RepoUrl   = "https://github.com/mochi-ms/Ropi.git",
    [string]$TargetDir = "",
    [switch]$WithAI,          # also install requirements-ai.txt (PyTorch etc., ~2GB)
    [switch]$SkipApps         # skip winget installs
)

$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[Ropi] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $extra   = @(
        "C:\Program Files\Git\cmd",
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin",
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts",
        "$env:LOCALAPPDATA\Programs\gh\bin",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
    ) -join ";"
    $env:Path = "$machine;$user;$extra"
}

function Test-Cmd($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Install-Winget($id, $cmd, [switch]$UserScope) {
    if ($cmd -and (Test-Cmd $cmd)) { Ok "$id already installed"; return $true }
    Info "Installing $id ..."
    $wgArgs = @("install", "--id", $id, "-e", "--source", "winget",
              "--accept-package-agreements", "--accept-source-agreements", "--silent")
    if ($UserScope) { $wgArgs += @("--scope", "user") }
    & winget @wgArgs
    $code = $LASTEXITCODE
    Refresh-Path
    if ($code -eq 0 -or ($cmd -and (Test-Cmd $cmd))) { Ok "$id installed"; return $true }
    Warn "$id install failed (exit $code)"
    return $false
}

function Install-MinGitPortable {
    Info "Installing portable MinGit (no admin required) ..."
    $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -match '^MinGit-[\d\.]+-64-bit\.zip$' } | Select-Object -First 1
    $dest = "$env:LOCALAPPDATA\Programs\MinGit"
    New-Item -ItemType Directory -Force $dest | Out-Null
    $zip = "$env:TEMP\mingit.zip"
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    Expand-Archive $zip -DestinationPath $dest -Force
    Remove-Item $zip -Force
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$dest\cmd*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$dest\cmd", "User")
    }
    Refresh-Path
    Ok "MinGit installed at $dest"
}

function Install-GhPortable {
    Info "Installing portable GitHub CLI (no admin required) ..."
    $rel = Invoke-RestMethod "https://api.github.com/repos/cli/cli/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -match '^gh_[\d\.]+_windows_amd64\.zip$' } | Select-Object -First 1
    $dest = "$env:LOCALAPPDATA\Programs\gh"
    New-Item -ItemType Directory -Force $dest | Out-Null
    $zip = "$env:TEMP\gh.zip"
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    Expand-Archive $zip -DestinationPath $dest -Force
    Remove-Item $zip -Force
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$dest\bin*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$dest\bin", "User")
    }
    Refresh-Path
    Ok "GitHub CLI installed at $dest"
}

Refresh-Path

# Resolve TargetDir automatically when run inside an existing clone
if (-not $TargetDir) {
    $scriptParent = Split-Path -Parent $PSScriptRoot
    if ($scriptParent -and (Test-Path (Join-Path $scriptParent "docs\SPEC.md"))) {
        $TargetDir = $scriptParent
    } else {
        $TargetDir = "$env:USERPROFILE\dev\Ropi"
    }
}

# ---------------------------------------------------------------- 1. Apps
if (-not $SkipApps) {
    if (-not (Test-Cmd "winget")) {
        throw "winget not found. Install 'App Installer' from Microsoft Store, then re-run."
    }
    if (-not (Install-Winget "Git.Git" "git")) { Install-MinGitPortable }
    Install-Winget "Microsoft.VisualStudioCode" "code" -UserScope | Out-Null
    Install-Winget "Python.Python.3.12" $null -UserScope | Out-Null
    Install-Winget "Gyan.FFmpeg" "ffmpeg" | Out-Null
    if (-not (Install-Winget "GitHub.cli" "gh")) { Install-GhPortable }
    Refresh-Path
}

# ---------------------------------------------------------------- 2. Python check
$py = $null
if (Test-Cmd "py") {
    & py -3.12 --version 2>$null
    if ($LASTEXITCODE -eq 0) { $py = @("py", "-3.12") }
}
if (-not $py) {
    $cand = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (Test-Path $cand) { $py = @($cand) }
}
if (-not $py) { throw "Python 3.12 not found. Re-open PowerShell and run again." }
Ok ("Python: " + (& $py[0] @($py | Select-Object -Skip 1) --version))

# ---------------------------------------------------------------- 3. Git identity
if (-not (git config --global user.name))  { git config --global user.name  (Read-Host "Git user.name") }
if (-not (git config --global user.email)) { git config --global user.email (Read-Host "Git user.email") }
git config --global core.autocrlf true
git config --global init.defaultBranch main

# ---------------------------------------------------------------- 4. Clone / Sync
if (-not (Test-Path "$TargetDir\.git")) {
    Info "Cloning $RepoUrl -> $TargetDir"
    New-Item -ItemType Directory -Force (Split-Path $TargetDir) | Out-Null
    git clone $RepoUrl $TargetDir
} else {
    Info "Repo exists at $TargetDir"
}
Set-Location $TargetDir

# ---------------------------------------------------------------- 5. VS Code extensions
if (Test-Cmd "code") {
    $exts = @(
        "ms-python.python", "ms-python.vscode-pylance", "ms-python.debugpy",
        "charliermarsh.ruff", "tamasfe.even-better-toml", "eamodio.gitlens",
        "seanwu.vscode-qt-for-python", "yzhang.markdown-all-in-one",
        "bierner.markdown-mermaid", "github.vscode-github-actions"
    )
    foreach ($e in $exts) { & code --install-extension $e --force | Out-Null }
    Ok "VS Code extensions installed"
} else { Warn "VS Code CLI 'code' not on PATH yet (re-open terminal)" }

# ---------------------------------------------------------------- 6. venv + packages
if (-not (Test-Path ".venv")) {
    Info "Creating .venv"
    & $py[0] @($py | Select-Object -Skip 1) -m venv .venv
}
$vpy = ".\.venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip wheel setuptools
foreach ($req in @("requirements.txt", "requirements-dev.txt")) {
    if (Test-Path $req) { & $vpy -m pip install -r $req }
}
& $vpy -m pip install -e . --no-deps
if ($WithAI -and (Test-Path "requirements-ai.txt")) {
    $hasNvidia = [bool](Get-Command "nvidia-smi" -ErrorAction SilentlyContinue)
    if ($hasNvidia) {
        Info "NVIDIA GPU detected -> CUDA PyTorch"
        & $vpy -m pip install torch --index-url https://download.pytorch.org/whl/cu124
    } else {
        Info "No NVIDIA GPU -> CPU PyTorch"
        & $vpy -m pip install torch --index-url https://download.pytorch.org/whl/cpu
    }
    & $vpy -m pip install -r requirements-ai.txt
}

# ---------------------------------------------------------------- 7. Summary
Write-Host ""
Ok "Setup complete."
foreach ($c in @("git", "code", "ffmpeg", "gh")) {
    $s = if (Test-Cmd $c) { "OK" } else { "MISSING (re-open terminal)" }
    Write-Host ("  {0,-8} {1}" -f $c, $s)
}
Write-Host "  venv     $TargetDir\.venv"
Write-Host ""
Write-Host "Next: 'gh auth login' (once), then 'code .' to open the project."
