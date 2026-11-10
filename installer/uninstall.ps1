# Hokma uninstaller for Windows (PowerShell 5.1+).
# Removes the runtime, the `hokma` command and the PATH entry.
# The student's working folder (and the Claude session kept in Hokma's config dir)
# are NEVER deleted without confirmation.
#
# Usage: powershell -ExecutionPolicy Bypass -File installer\uninstall.ps1 [-Dir <workdir>] [-Lang es|en] [-Purge | -ResetAll]
#   -Purge   also delete the working folder and the Claude session; type the word shown in the prompt, or set
#            HOKMA_CONFIRM_PURGE to it when not interactive.
#   -ResetAll  used by Hokma-Setup.exe ("Reinstall from scratch"): delete the WHOLE data folder (runtime, Python,
#            cache, update.lock and the Claude session) without asking. The working folder is NEVER touched (the
#            setup already moved it to a backup). Exit code 1 when the data folder could not be deleted.

param(
    [string]$Dir = $(if ($env:HOKMA_DIR) { $env:HOKMA_DIR } else { Join-Path $env:USERPROFILE "Hokma" }),
    [string]$Lang = "",
    [switch]$Purge,
    [switch]$ResetAll
)

# A parent PowerShell 7 hands its PSModulePath down; Windows PowerShell 5.1 then cannot load its own modules
# (the hashing, web request and JSON cmdlets vanish). Put back the 5.1 default, for this process and its children.
if ($PSVersionTable.PSEdition -ne "Core") {
    $env:PSModulePath = (Join-Path $env:USERPROFILE "Documents\WindowsPowerShell\Modules") + ";" +
        (Join-Path $env:ProgramFiles "WindowsPowerShell\Modules") + ";" +
        (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules")
}

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "messages.ps1")

$BinDir = Join-Path $env:USERPROFILE ".local\bin"
$launcher = Join-Path $BinDir "hokma.cmd"
$Data = if ($env:HOKMA_DATA_DIR) { $env:HOKMA_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "Hokma" }

# Same language as the installation unless told otherwise.
if (-not $env:HOKMA_LANG) {
    $saved = Join-Path $Data "language"
    if (Test-Path $saved) {
        $code = (Get-Content $saved -Raw).Trim()
        if (@("es", "en") -contains $code) { $script:LangCode = $code }
    }
}
if ($Lang) {
    if (@("es", "en") -notcontains $Lang) { Die "inst.lang_invalid" $Lang }
    $script:LangCode = $Lang
}

# If the launcher knows the folders, trust it over the defaults (it is UTF-8).
if (Test-Path $launcher) {
    if (-not $env:HOKMA_DIR -and -not $PSBoundParameters.ContainsKey("Dir")) {
        $line = Select-String -Path $launcher -Encoding UTF8 -Pattern '^set "HOKMA_HOME=(.*)"$' | Select-Object -First 1
        if ($line) { $Dir = $line.Matches[0].Groups[1].Value }
    }
    if (-not $env:HOKMA_DATA_DIR) {
        $line = Select-String -Path $launcher -Encoding UTF8 -Pattern '^set "HOKMA_DATA_DIR=(.*)"$' | Select-Object -First 1
        if ($line) { $Data = $line.Matches[0].Groups[1].Value }
    }
}

if (Test-Path $launcher) { Remove-Item -Force $launcher; Info "un.command_removed" }

# Shortcuts (Start menu and Desktop), in every language: the names come from the installer message files.
$shortcutFolders = @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop")) | Where-Object { $_ }
foreach ($code in @("es", "en")) {
    $table = Get-MsgTable $code
    foreach ($key in @("shortcut.start", "shortcut.legacy.stop", "shortcut.legacy.restart")) {
        $name = $table[$key]
        if (-not $name) { continue }
        foreach ($folder in $shortcutFolders) {
            $path = Join-Path $folder ($name + ".lnk")
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
        }
    }
}
Info "un.shortcuts_removed"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath) {
    $parts = $userPath -split ";" | Where-Object { $_ -and ($_ -ne $BinDir) }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
    Info "un.path_removed_win" $BinDir
}

if ($ResetAll) {
    # Processes may need a moment to release their files after being closed: up to 5 attempts, 1 second apart.
    for ($attempt = 1; ($attempt -le 5) -and (Test-Path -LiteralPath $Data); $attempt++) {
        try { Remove-Item -LiteralPath $Data -Recurse -Force -ErrorAction Stop }
        catch { Start-Sleep -Seconds 1 }
    }
    if (Test-Path -LiteralPath $Data) { Die "un.reset_failed" $Data }
    Info "un.reset_done" $Data
    Info "un.workdir_kept" $Dir
    Info "un.done"
    exit 0
}

# Ask for the destructive confirmation once.
$confirmed = $false
$purgeWord = Msg "un.purge_word"
if ($Purge) {
    if ($env:HOKMA_CONFIRM_PURGE -eq $purgeWord) { $confirmed = $true }
    elseif ([Environment]::UserInteractive) {
        $answer = Read-Host (Msg "un.purge_prompt" @($Dir, $purgeWord))
        if ($answer -ceq $purgeWord) { $confirmed = $true }
    }
}

if (Test-Path $Data) {
    Get-ChildItem -Path $Data -Force | Where-Object { $_.Name -ne "claude-config" } | Remove-Item -Recurse -Force
    Info "un.runtime_removed" $Data
    if ($confirmed) { Remove-Item -Recurse -Force $Data }
    else { Info "un.session_kept" (Join-Path $Data "claude-config") }
}

if (Test-Path $Dir) {
    if ($confirmed) { Remove-Item -Recurse -Force $Dir; Info "un.workdir_removed" }
    else { Info "un.workdir_kept" $Dir }
}
Info "un.done"
