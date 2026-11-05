# Hokma installer for Windows (PowerShell 5.1+). No administrator rights needed.
#
# Usage (from a checkout of the repo):
#   powershell -ExecutionPolicy Bypass -File installer\install.ps1 [-Dir <workdir>] [-Lang es|en] [-NoDoctor]
#
# Folders:
#   workdir (default %USERPROFILE%\Hokma)   the student's folder, where the agent works
#   data    (default %LOCALAPPDATA%\Hokma)  runtime + Claude config, OUTSIDE the workdir
#
# This file is ASCII on purpose (Windows PowerShell 5.1 reads BOM-less files as ANSI). Messages come from
# installer\messages\<lang>.properties (UTF-8), loaded by messages.ps1.

param(
    [string]$Dir = $(if ($env:HOKMA_DIR) { $env:HOKMA_DIR } else { Join-Path $env:USERPROFILE "Hokma" }),
    [string]$Lang = "",
    [switch]$NoDoctor
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

. (Join-Path $PSScriptRoot "messages.ps1")
if ($Lang) {
    if (@("es", "en") -notcontains $Lang) { Die "inst.lang_invalid" $Lang }
    $script:LangCode = $Lang
}

# Run git without letting its stderr (CRLF warnings, hints) trigger
# ErrorActionPreference=Stop in Windows PowerShell 5.1. Success is judged by exit code only.
function Invoke-Git {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & git @args 2>&1
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
    $script:GitOutput = ($out | ForEach-Object { "$_" }) -join "`n"
    return $code
}

# ---- Stage 0: preconditions -------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -and -not $env:HOKMA_ALLOW_ADMIN) {
    Die "inst.no_admin"
}

$Src = Split-Path -Parent $PSScriptRoot
$Lock = if ($env:HOKMA_LOCK_FILE) { $env:HOKMA_LOCK_FILE } else { Join-Path $PSScriptRoot "lock.json" }
if (-not (Test-Path $Lock)) { Die "inst.no_lock" $Lock }
if (-not (Test-Path (Join-Path $Src "pyproject.toml"))) { Die "inst.not_checkout" }

$Dir = [System.IO.Path]::GetFullPath($Dir)
$HokmaData = if ($env:HOKMA_DATA_DIR) { $env:HOKMA_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "Hokma" }
$HokmaData = [System.IO.Path]::GetFullPath($HokmaData)
$dirSlash = $Dir.TrimEnd("\") + "\"
$dataSlash = $HokmaData.TrimEnd("\") + "\"
if ($dataSlash.StartsWith($dirSlash, [System.StringComparison]::OrdinalIgnoreCase) -or
    $dirSlash.StartsWith($dataSlash, [System.StringComparison]::OrdinalIgnoreCase)) {
    Die "inst.overlap" $HokmaData $Dir
}
$BinDir = Join-Path $env:USERPROFILE ".local\bin"
$ConfigDir = Join-Path $HokmaData "claude-config"

# ---- Stage 1: platform and lock.json ---------------------------------------
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -eq "ARM64") { $platform = "windows-aarch64" }
elseif ($arch -eq "AMD64") { $platform = "windows-x86_64" }
else { Die "inst.bad_arch" $arch }

$lockData = Get-Content $Lock -Raw | ConvertFrom-Json
$entry = $lockData.platforms.$platform
if (-not $entry) { Die "inst.no_platform" $platform }
$UvVersion = $lockData.uv_version
$PyVersion = $lockData.python_version
Info "inst.platform" $platform $Dir

# ---- Stage 2: dependencies (never installed behind the student's back) ------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "inst.git_missing_win" }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Warn "inst.claude_missing"
    Warn "inst.claude_install_win"
    Warn "inst.claude_guide"
}

# ---- Stage 3: uv (sha256 verified before anything is executed) --------------
New-Item -ItemType Directory -Force -Path (Join-Path $HokmaData "bin"), (Join-Path $HokmaData "tmp") | Out-Null
[System.IO.File]::WriteAllText((Join-Path $HokmaData "language"), $script:LangCode + "`n", (New-Object System.Text.UTF8Encoding($false)))
$Tmp = Join-Path $HokmaData ("tmp\dl-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
try {
    $archive = Join-Path $Tmp "uv.zip"
    Info "inst.download_uv" $UvVersion
    try { Invoke-WebRequest -Uri $entry.url -OutFile $archive -UseBasicParsing }
    catch { Die "inst.download_failed" $entry.url }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLower()
    if ($actual -ne $entry.sha256.ToLower()) { Die "inst.sha_mismatch" $entry.sha256 $actual }
    Expand-Archive -Path $archive -DestinationPath $Tmp -Force
    $uvExe = Get-ChildItem -Path $Tmp -Recurse -Filter "uv.exe" | Select-Object -First 1
    if (-not $uvExe) { Die "inst.uv_not_in_package" }
    Copy-Item $uvExe.FullName (Join-Path $HokmaData "bin\uv.exe") -Force
}
finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
$Uv = Join-Path $HokmaData "bin\uv.exe"

# ---- Stage 4: Python and Hokma app, built in the inactive slot --------------
# Blue-green runtime: <data>\runtime\a and \b. The new version is built in the slot that is NOT active
# and only then does `current` flip (atomic replace), so a failed install keeps the previous version.
$active = ""
$pointer = Join-Path $HokmaData "current"
if (Test-Path $pointer) { $active = (Get-Content $pointer -Raw).Trim() }
$Slot = if ($active -eq "a") { "b" } else { "a" }
$SlotDir = Join-Path $HokmaData ("runtime\" + $Slot)
$App = Join-Path $SlotDir "app"
$Venv = Join-Path $SlotDir "venv"

Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue
$env:UV_NO_CONFIG = "1"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $HokmaData "python"
$env:UV_CACHE_DIR = Join-Path $HokmaData "cache"
$env:UV_PYTHON_PREFERENCE = "only-managed"
$env:UV_PROJECT_ENVIRONMENT = $Venv

Info "inst.install_python" $PyVersion
& $Uv python install $PyVersion
if ($LASTEXITCODE -ne 0) { Die "inst.python_failed" $PyVersion }

Info "inst.copy_app"
if (Test-Path $SlotDir) { Remove-Item -Recurse -Force $SlotDir }
New-Item -ItemType Directory -Force -Path $App | Out-Null
& robocopy $Src $App /E /XD .git node_modules .venv __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Die "inst.copy_failed" $LASTEXITCODE }
if (-not (Test-Path (Join-Path $App "dashboard\dist\index.html"))) { Die "inst.no_dashboard" }

Info "inst.install_deps"
& $Uv sync --project $App --frozen --no-dev --python $PyVersion
if ($LASTEXITCODE -ne 0) { Die "inst.deps_failed" }

$env:HOKMA_HOME = $Dir
$env:HOKMA_DATA_DIR = $HokmaData
$env:CLAUDE_CONFIG_DIR = $ConfigDir
$HokmaExe = Join-Path $Venv "Scripts\hokma.exe"
# Integrity manifest (sha256 of app, which includes the settings template). hokma and doctor verify it.
& $HokmaExe write-manifest
if ($LASTEXITCODE -ne 0) { Die "inst.manifest_failed" }
& $HokmaExe verify-install | Out-Null
if ($LASTEXITCODE -ne 0) { Die "inst.verify_failed" }

# ---- Stage 5: working folder as a Git repo, kit (never overwriting the student), config
New-Item -ItemType Directory -Force -Path $Dir, $ConfigDir | Out-Null
& $HokmaExe kit-sync | Out-Null
if ($LASTEXITCODE -ne 0) { Die "inst.kit_failed" }
$gi = Join-Path $Dir ".gitignore"
if (-not (Test-Path $gi)) {
    Set-Content -Path $gi -Value @("*.db", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm", ".env") -Encoding ASCII
}
# git runs ONLY here, on a repo that does not exist yet. If anything named .git is there (folder, file or link,
# even a broken one) it is the student's repo and Hokma never runs git on it.
if (-not (Get-Item -LiteralPath (Join-Path $Dir ".git") -Force -ErrorAction SilentlyContinue)) {
    [void](Invoke-Git -C $Dir init -q)
    # Local setting for this repo only: avoids the CRLF warnings on every git add.
    [void](Invoke-Git -C $Dir config core.autocrlf true)
    $code = Invoke-Git -C $Dir add -A
    if ($code -ne 0) { Warn "inst.git_add_failed" $Dir $GitOutput }
    [void](Invoke-Git -C $Dir config user.name)
    $idArgs = @()
    if (-not $GitOutput.Trim()) { $idArgs = @("-c", "user.name=Hokma", "-c", "user.email=hokma@localhost") }
    $code = Invoke-Git -C $Dir @idArgs commit -q -m "Hokma: initial kit"
    if ($code -ne 0) { Warn "inst.commit_failed" $Dir $GitOutput }
}

# ---- Stage 6: launcher, atomic switch to the new slot, settings, PATH --------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
# UTF-8 without BOM plus `chcp 65001`, so folders with accents or enye (C:\Users\Jose with tilde) survive.
# The launcher reads the active slot from <data>\current at run time.
$launcherError = Msg "inst.launcher_error"
$launcher = @(
    "@echo off",
    "rem Generated by the Hokma installer. Do not edit.",
    "setlocal",
    "for /f `"tokens=2 delims=:.`" %%c in ('chcp') do set `"HOKMA_OLDCP=%%c`"",
    "chcp 65001 >nul",
    "set `"HOKMA_HOME=$Dir`"",
    "set `"HOKMA_DATA_DIR=$HokmaData`"",
    "set `"CLAUDE_CONFIG_DIR=$ConfigDir`"",
    "set /p HOKMA_SLOT=<`"%HOKMA_DATA_DIR%\current`"",
    "if `"%HOKMA_SLOT%`"==`"a`" goto run",
    "if `"%HOKMA_SLOT%`"==`"b`" goto run",
    "echo [error] $launcherError 1>&2",
    "chcp %HOKMA_OLDCP% >nul",
    "exit /b 1",
    ":run",
    "`"%HOKMA_DATA_DIR%\runtime\%HOKMA_SLOT%\venv\Scripts\hokma.exe`" %*",
    "set `"HOKMA_RC=%ERRORLEVEL%`"",
    "chcp %HOKMA_OLDCP% >nul",
    "exit /b %HOKMA_RC%"
)
$launcherPath = Join-Path $BinDir "hokma.cmd"
[System.IO.File]::WriteAllLines($launcherPath, $launcher, (New-Object System.Text.UTF8Encoding($false)))

# Atomic switch: write a temp file, then replace `current`.
$tmpPointer = Join-Path $HokmaData "current.tmp"
[System.IO.File]::WriteAllText($tmpPointer, $Slot + "`n", (New-Object System.Text.UTF8Encoding($false)))
Move-Item -Force -Path $tmpPointer -Destination $pointer

# Runtime and Claude config live OUTSIDE the working folder; lock them from the agent.
& $HokmaExe init-config | Out-Null
if ($LASTEXITCODE -ne 0) { Die "inst.config_failed" }

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ";") -notcontains $BinDir) {
    [Environment]::SetEnvironmentVariable("Path", ($BinDir + ";" + $userPath).TrimEnd(";"), "User")
    Info "inst.path_added_win" $BinDir
}

# ---- Stage 7: doctor --------------------------------------------------------
Info "inst.installed" $Dir
if (-not $NoDoctor) {
    Info "inst.running_doctor"
    & $launcherPath doctor
    if ($LASTEXITCODE -ne 0) { Warn "inst.doctor_problems" }
}
Info "inst.done"
