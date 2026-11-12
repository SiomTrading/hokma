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

# A parent PowerShell 7 hands its PSModulePath down; Windows PowerShell 5.1 then cannot load its own modules
# (the hashing, web request and JSON cmdlets vanish). Put back the 5.1 default, for this process and its children.
if ($PSVersionTable.PSEdition -ne "Core") {
    $env:PSModulePath = (Join-Path $env:USERPROFILE "Documents\WindowsPowerShell\Modules") + ";" +
        (Join-Path $env:ProgramFiles "WindowsPowerShell\Modules") + ";" +
        (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules")
}

. (Join-Path $PSScriptRoot "messages.ps1")
. (Join-Path $PSScriptRoot "download.ps1")
. (Join-Path $PSScriptRoot "native.ps1")
if ($Lang) {
    if (@("es", "en") -notcontains $Lang) { Die "inst.lang_invalid" $Lang }
    $script:LangCode = $Lang
}

# SHA256 with .NET, so it does not depend on the Microsoft.PowerShell.Utility module, which provides Get-FileHash.
function Get-Sha256([string]$path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($path)
    try { $bytes = $sha.ComputeHash($stream) }
    finally { $stream.Dispose(); $sha.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

# git through Invoke-Native (its stderr never stops the script). Success is judged by exit code only; the output
# stays in $GitOutput.
function Invoke-Git {
    $code = Invoke-Native "git" ([string[]]$args) -Quiet
    $script:GitOutput = $script:NativeOutput
    return $code
}

# ---- Stage 0: preconditions -------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$IsServer = Test-WindowsServer
Info "inst.windows_edition" (Get-WindowsEdition)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -and -not $env:HOKMA_ALLOW_ADMIN) {
    # Windows Server: the usual user IS an Administrator. The install goes on (it is per user anyway); it is only logged.
    if ($IsServer) { Warn "inst.server_admin" } else { Die "inst.no_admin" }
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
# OneDrive deletes and locks files that Hokma needs (temporary files, the database): never install inside it.
foreach ($oneDrive in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
    if (-not $oneDrive) { continue }
    $oneDriveSlash = [System.IO.Path]::GetFullPath($oneDrive).TrimEnd("\") + "\"
    if ($dirSlash.StartsWith($oneDriveSlash, [System.StringComparison]::OrdinalIgnoreCase)) { Die "inst.onedrive" $Dir }
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
# Hokma-Setup.exe may have installed Git a moment ago: read PATH again from the registry (machine and user).
$env:Path = (@([Environment]::GetEnvironmentVariable("Path", "Machine"),
               [Environment]::GetEnvironmentVariable("Path", "User"), $env:Path) | Where-Object { $_ }) -join ";"
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
    if (-not (Invoke-HokmaDownload $entry.url $archive ("uv " + $UvVersion) { param($t) Write-Host $t })) {
        Die "inst.download_failed" $entry.url $script:DownloadError
    }
    $actual = Get-Sha256 $archive
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
$PyRoot = $env:UV_PYTHON_INSTALL_DIR
# uv has no option to skip the junction, and it makes it again on every `uv python install`, even when the Python is
# already there (it downloads nothing). So uv is asked only when no real Python of this version is installed.
$PyExe = Get-RealPython $PyRoot $PyVersion
$code = 0
if (-not $PyExe) { $code = Invoke-Native $Uv @("python", "install", $PyVersion) }
# uv also makes a junction (cpython-3.12-...) to the real folder. Windows Redirection Guard can refuse to cross it
# (WinError 448), so the venv is built from the real folder and the junction is removed. If uv only failed on the
# junction and the real Python is there, the install goes on.
$PyExe = Get-RealPython $PyRoot $PyVersion
if ($code -ne 0 -and -not $PyExe) { Die "inst.python_failed" $PyVersion }
Remove-PythonLinks $PyRoot
$PySpec = if ($PyExe) { $PyExe } else { $PyVersion }

Info "inst.copy_app"
if (Test-Path $SlotDir) { Remove-Item -Recurse -Force $SlotDir }
New-Item -ItemType Directory -Force -Path $App | Out-Null
$code = Invoke-Native "robocopy" @($Src, $App, "/E", "/XD", ".git", "node_modules", ".venv", "__pycache__", "/NFL", "/NDL", "/NJH", "/NJS", "/NP") -Quiet
if ($code -ge 8) { Die "inst.copy_failed" $code }
if (-not (Test-Path (Join-Path $App "dashboard\dist\index.html"))) { Die "inst.no_dashboard" }

Info "inst.install_deps"
$code = Invoke-Native $Uv @("sync", "--project", $App, "--frozen", "--no-dev", "--python", $PySpec)
if ($code -ne 0) { Die "inst.deps_failed" }

$env:HOKMA_HOME = $Dir
$env:HOKMA_DATA_DIR = $HokmaData
$env:CLAUDE_CONFIG_DIR = $ConfigDir
$HokmaExe = Join-Path $Venv "Scripts\hokma.exe"
# Integrity manifest (sha256 of app, which includes the settings template). hokma and doctor verify it.
$code = Invoke-Native $HokmaExe @("write-manifest")
if ($code -ne 0) { Die "inst.manifest_failed" }
$code = Invoke-Native $HokmaExe @("verify-install") -Quiet
if ($code -ne 0) { Die "inst.verify_failed" }

# Node and Bun, portable, in <data>\tools (versions and sha256 in lock.json, checked before unpacking). Plugins and their
# hooks need them (claude-mem starts its worker with Bun); Hokma puts them on the PATH of the engine. No network or a
# wrong hash installs nothing: the install goes on and `hokma install-tools` repeats it.
Info "inst.install_tools"
$code = Invoke-Native $HokmaExe @("install-tools")
if ($code -ne 0) { Warn "inst.tools_failed" }

# ---- Stage 5: working folder as a Git repo, kit (never overwriting the student), config
New-Item -ItemType Directory -Force -Path $Dir, $ConfigDir | Out-Null
$code = Invoke-Native $HokmaExe @("kit-sync") -Quiet
if ($code -ne 0) { Die "inst.kit_failed" }
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
$code = Invoke-Native $HokmaExe @("init-config") -Quiet
if ($code -ne 0) { Die "inst.config_failed" }

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ";") -notcontains $BinDir) {
    [Environment]::SetEnvironmentVariable("Path", ($BinDir + ";" + $userPath).TrimEnd(";"), "User")
    Info "inst.path_added_win" $BinDir
}

# Shortcut: Hokma (Start menu and Desktop). Old Shut down and Restart shortcuts are removed. One implementation, shared with
# `hokma update`: `hokma shortcuts` (hokma.core.shortcuts). They point at the stable launcher, run without a console
# window, and there is NO start at login: Hokma starts when the owner opens the "Hokma" shortcut.
$code = Invoke-Native $HokmaExe @("shortcuts", "--force")
if ($code -eq 0) { Info "inst.shortcuts_created" } else { Warn "inst.shortcuts_failed" }

# ---- Stage 7: doctor --------------------------------------------------------
Info "inst.installed" $Dir
if (-not $NoDoctor) {
    Info "inst.running_doctor"
    $code = Invoke-Native $launcherPath @("doctor")
    if ($code -ne 0) { Warn "inst.doctor_problems" }
}
Info "inst.done"
