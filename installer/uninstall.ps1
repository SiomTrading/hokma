# Hokma uninstaller for Windows (PowerShell 5.1+).
# Removes the runtime, the `hokma` command and the PATH entry.
# The student's working folder (and the Claude session kept in Hokma's config dir)
# are NEVER deleted without confirmation.
#
# Usage: powershell -ExecutionPolicy Bypass -File installer\uninstall.ps1 [-Dir <workdir>] [-Lang es|en] [-Purge]
#   -Purge   also delete the working folder and the Claude session; type the word shown in the prompt, or set
#            HOKMA_CONFIRM_PURGE to it when not interactive.

param(
    [string]$Dir = $(if ($env:HOKMA_DIR) { $env:HOKMA_DIR } else { Join-Path $env:USERPROFILE "Hokma" }),
    [string]$Lang = "",
    [switch]$Purge
)

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

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath) {
    $parts = $userPath -split ";" | Where-Object { $_ -and ($_ -ne $BinDir) }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
    Info "un.path_removed_win" $BinDir
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
