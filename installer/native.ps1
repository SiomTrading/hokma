# Runs native programs (uv, git, node, bun, claude, python, hokma.exe, robocopy, winget...) for install.ps1 and
# uninstall.ps1 (dot-source it, do not run it). Task 3172.
#
# Why: Windows PowerShell 5.1 with $ErrorActionPreference = "Stop" turns EVERY line a native program writes to stderr
# into a terminating NativeCommandError, even when the program succeeds. uv writes its progress ("Using CPython
# 3.12.13") to stderr, so `& $Uv sync` stopped the install of 0.0.23 on Windows Server with exit code 1.
#
# Invoke-Native lowers $ErrorActionPreference to Continue only during the call, merges stderr into stdout, forwards each
# line (as plain text, never as an error record) to the log, and decides by $LASTEXITCODE alone. It returns the exit
# code; the whole output is kept in $script:NativeOutput. Every native call of the installer goes through it.
#
# This file is ASCII on purpose (Windows PowerShell 5.1 reads BOM-less files as ANSI).

$script:NativeOutput = ""

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$Arguments = @(),
        [switch]$Quiet
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $lines = New-Object System.Collections.Generic.List[string]
    $code = 0
    try {
        $global:LASTEXITCODE = 0
        & $File @Arguments 2>&1 | ForEach-Object {
            $text = "$_"
            $lines.Add($text)
            if (-not $Quiet -and $text.Trim()) { Write-Host $text }
        }
        $code = $LASTEXITCODE
    }
    catch {
        $lines.Add($_.Exception.Message)
        if (-not $Quiet) { Write-Host $_.Exception.Message }
        $code = 1
    }
    finally { $ErrorActionPreference = $prev }
    if ($null -eq $code) { $code = 0 }
    $script:NativeOutput = ($lines.ToArray()) -join "`n"
    return [int]$code
}

# The interpreter of the newest REAL patch folder (cpython-3.12.13-...) under uv's Python folder, "" when there is none.
# Never the minor version junction (cpython-3.12-...): Windows Redirection Guard can refuse to cross it (WinError 448).
function Get-RealPython {
    param([string]$Root, [string]$Version)
    $minor = (($Version -split "\.")[0..1]) -join "."
    if (-not (Test-Path -LiteralPath $Root)) { return "" }
    $best = ""
    $bestPatch = -1
    foreach ($d in (Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
        if ($d.Name -notmatch ("^cpython-" + [regex]::Escape($minor) + "\.(\d+)-")) { continue }
        $exe = Join-Path $d.FullName "python.exe"
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        $patch = [int]$Matches[1]
        if ($patch -gt $bestPatch) { $best = $exe; $bestPatch = $patch }
    }
    return $best
}

# Removes the links (junctions) directly under uv's Python folder. Deleting a junction removes only the link, never the
# folder it points to.
function Remove-PythonLinks {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) { return }
    foreach ($d in (Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($d.Name -like "cpython-*" -and ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            try { [System.IO.Directory]::Delete($d.FullName, $false) } catch { }
        }
    }
}

# Windows edition, for the log. Windows Server is supported: the install goes on, the edition is only recorded.
function Get-WindowsEdition {
    try {
        $k = "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        $get = [Microsoft.Win32.Registry]
        return ("" + $get::GetValue($k, "ProductName", "") + " (" + $get::GetValue($k, "EditionID", "") + ", " +
                $get::GetValue($k, "InstallationType", "") + ") build " + $get::GetValue($k, "CurrentBuildNumber", ""))
    }
    catch { return ([Environment]::OSVersion.VersionString) }
}

function Test-WindowsServer {
    try {
        $k = "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        $type = "" + [Microsoft.Win32.Registry]::GetValue($k, "InstallationType", "")
        $name = "" + [Microsoft.Win32.Registry]::GetValue($k, "ProductName", "")
        return (($type -like "Server*") -or ($name -like "*Server*"))
    }
    catch { return $false }
}
