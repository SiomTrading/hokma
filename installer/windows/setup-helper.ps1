# Stages of Hokma-Setup.exe (Windows PowerShell 5.1+), run hidden by the Inno Setup script hokma-setup.iss.
#
# The setup window shows the progress; this script does the work and talks back on standard output, one line each:
#   STAGE:<name>      a stage starts (closing, backup, reset, git, claude, install, restore, shortcuts, webview)
#   LINE:<text>       a line of install.ps1 (shown under the progress bar)
#   RESULT:<k>=<v>    a fact the setup needs later (backup path, claude_failed, ...)
#   ERROR:<text>      why the run stopped (English, for the log; the setup shows its own message)
# Exit code 0 = done; anything else = failed. Nothing here ever deletes the student's working folder.
#
# This file is ASCII on purpose (Windows PowerShell 5.1 reads BOM-less files as ANSI). The student never reads it.

param(
    [ValidateSet("Detect", "Run", "Finish", "Stop", "WaitPid")]
    [string]$Action = "Run",
    [ValidateSet("fresh", "update", "reinstall")]
    [string]$Mode = "fresh",
    [string]$Dir = "",
    [string]$Src = "",
    [string]$Lang = "es",
    [string]$BackupWord = "backup",
    [string]$Out = "",
    [string]$Log = "",
    [string]$AppDir = "",
    [string]$WebView2 = "",
    [int]$WaitFor = 0,
    [switch]$InstallClaude,
    [switch]$NoApp
)

# A parent PowerShell 7 hands its PSModulePath down; Windows PowerShell 5.1 then cannot load its own modules
# (the hashing, web request and JSON cmdlets vanish). Put back the 5.1 default, for this process and its children.
if ($PSVersionTable.PSEdition -ne "Core") {
    $env:PSModulePath = (Join-Path $env:USERPROFILE "Documents\WindowsPowerShell\Modules") + ";" +
        (Join-Path $env:ProgramFiles "WindowsPowerShell\Modules") + ";" +
        (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules")
}

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# SHA256 with .NET, so it does not depend on the Microsoft.PowerShell.Utility module.
function Get-Sha256([string]$path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($path)
    try { $bytes = $sha.ComputeHash($stream) }
    finally { $stream.Dispose(); $sha.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

$BinDir = Join-Path $env:USERPROFILE ".local\bin"
$Launcher = Join-Path $BinDir "hokma.cmd"
$SetupHome = Join-Path $env:LOCALAPPDATA "Hokma-Setup"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8
# What "Reinstall from scratch" keeps from the data folder besides the working folder: the plugin list and the
# fund's database (SQLite file, its WAL files, its backups, the Postgres choice) and the owner and fund names.
# The runtime, Python, caches, update.lock and the Claude session are deleted.
$KeepItems = @("extensions.json", "hokma.db", "hokma.db-wal", "hokma.db-shm", "backups", "database.json", "owner.json")

function Write-Log([string]$text) {
    if (-not $Log) { return }
    try { [System.IO.File]::AppendAllText($Log, (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " " + $text + "`r`n", $Utf8) }
    catch { }
}

function Emit([string]$line) {
    [Console]::Out.WriteLine($line)
    [Console]::Out.Flush()
    Write-Log $line
}

function Fail([string]$why) {
    Emit ("ERROR:" + $why)
    exit 1
}

# Run a native program hidden, forward every output line as LINE:, return its exit code. Its stderr never stops
# this script (Windows PowerShell 5.1 turns stderr lines into errors under ErrorActionPreference=Stop).
function Invoke-Logged([string]$file, [string[]]$arguments) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $file @arguments 2>&1 | ForEach-Object { $text = "$_"; if ($text.Trim()) { Emit ("LINE:" + $text) } }
        $code = $LASTEXITCODE
    }
    catch { Emit ("LINE:" + $_.Exception.Message); $code = 1 }
    finally { $ErrorActionPreference = $prev }
    if ($null -eq $code) { $code = 0 }
    return $code
}

function Get-Platform {
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { return "windows-aarch64" }
    return "windows-x86_64"
}

function Update-PathFromRegistry {
    $env:Path = (@([Environment]::GetEnvironmentVariable("Path", "Machine"),
                   [Environment]::GetEnvironmentVariable("Path", "User"), $env:Path) | Where-Object { $_ }) -join ";"
}

function Test-Git { Update-PathFromRegistry; return [bool](Get-Command git -ErrorAction SilentlyContinue) }

function Test-Claude {
    Update-PathFromRegistry
    if (Get-Command claude -ErrorAction SilentlyContinue) { return $true }
    return (Test-Path -LiteralPath (Join-Path $env:USERPROFILE ".local\bin\claude.exe"))
}

# Folders the launcher was generated with (UTF-8), the same rule as uninstall.ps1.
function Read-LauncherValue([string]$name) {
    if (-not (Test-Path -LiteralPath $Launcher)) { return "" }
    $line = Select-String -LiteralPath $Launcher -Encoding UTF8 -Pattern ('^set "' + $name + '=(.*)"$') | Select-Object -First 1
    if ($line) { return $line.Matches[0].Groups[1].Value }
    return ""
}

function Test-Busy([string]$path) {
    try { $stream = [System.IO.File]::Open($path, "Open", "ReadWrite", "None"); $stream.Close(); return $false }
    catch { return $true }
}

$Data = Read-LauncherValue "HOKMA_DATA_DIR"
if (-not $Data) { $Data = if ($env:HOKMA_DATA_DIR) { $env:HOKMA_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "Hokma" } }
$Data = [System.IO.Path]::GetFullPath($Data)

# The working folder: the one chosen in the app (<data>\workdir) wins over the launcher's HOKMA_HOME.
function Get-WorkFolder {
    $pointer = Join-Path $Data "workdir"
    if (Test-Path -LiteralPath $pointer) {
        $value = ([System.IO.File]::ReadAllText($pointer, $Utf8)).Trim()
        if ($value -and [System.IO.Path]::IsPathRooted($value)) { return $value }
    }
    return (Read-LauncherValue "HOKMA_HOME")
}

# ---- stages ---------------------------------------------------------------------------------------------------

function Wait-ForPid([int]$processId) {
    if ($processId -le 0) { return }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 300
    }
    Write-Log ("process " + $processId + " still alive after 60 s; closing it")
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

# hokma stop first (30 s), then every process whose executable or command line is under the runtime, the managed
# Python or the desktop app folder, two passes. Works with every version, even those without `hokma stop`.
function Stop-Hokma {
    if (Test-Path -LiteralPath $Launcher) {
        $stop = Start-Process -FilePath "cmd.exe" -ArgumentList ('/d /c ""' + $Launcher + '" stop"') -WindowStyle Hidden -PassThru
        if (-not $stop.WaitForExit(30000)) { try { $stop.Kill() } catch { } }
    }
    $roots = @(((Join-Path $Data "runtime").TrimEnd("\") + "\"), ((Join-Path $Data "python").TrimEnd("\") + "\"))
    # The desktop app folder is matched by executable only: the setup and the uninstaller name it on their command
    # line and must never be closed by this.
    $appRoot = if ($AppDir) { [System.IO.Path]::GetFullPath($AppDir).TrimEnd("\") + "\" } else { "" }
    foreach ($attempt in 1..2) {
        $left = @(Get-CimInstance Win32_Process | Where-Object {
            $proc = $_
            if ($proc.ProcessId -eq $PID) { return $false }
            $exe = "" + $proc.ExecutablePath
            if ($appRoot -and $exe.StartsWith($appRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
                -not ([System.IO.Path]::GetFileName($exe) -like "unins*")) { return $true }
            foreach ($r in $roots) {
                if ($exe.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
                if ($proc.CommandLine -and $proc.CommandLine.IndexOf($r, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
            }
            return $false
        })
        foreach ($proc in $left) {
            Write-Log ("closing " + $proc.ProcessId + " " + $proc.ExecutablePath)
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if ($left.Count -gt 0) { Start-Sleep -Milliseconds 800 }
    }
}

# An update.lock whose PID is not alive was left by a crashed update.
function Remove-StaleLock {
    $lock = Join-Path $Data "update.lock"
    if (-not (Test-Path -LiteralPath $lock)) { return }
    $owner = 0
    [void][int]::TryParse(((Get-Content -LiteralPath $lock -Raw -ErrorAction SilentlyContinue) + "").Trim(), [ref]$owner)
    if ($owner -gt 0 -and (Get-Process -Id $owner -ErrorAction SilentlyContinue)) { Fail ("update in progress, pid " + $owner) }
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
    Write-Log "stale update.lock removed"
}

# The working folder is renamed on the same disk (instant, no copy). Returns the backup path, or "" when there was
# no folder.
function Backup-WorkFolder([string]$workdir) {
    if (-not (Test-Path -LiteralPath $workdir)) { return "" }
    $base = $workdir.TrimEnd("\") + "-" + $BackupWord + "-" + (Get-Date -Format "yyyyMMdd-HHmm")
    $target = $base
    $n = 2
    while (Test-Path -LiteralPath $target) { $target = $base + "-" + $n; $n++ }
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            [System.IO.Directory]::Move($workdir, $target)
            $script:PendingBackup = $target
            Emit ("RESULT:backup=" + $target)
            return $target
        }
        catch { Write-Log ("rename attempt " + $attempt + ": " + $_.Exception.Message); Start-Sleep -Seconds 1 }
    }
    Fail ("could not rename " + $workdir + " (in use); nothing was deleted")
}

# Copy the kept items of the data folder (plugins, database, names) aside BEFORE anything is deleted. A copy, not a
# move: it works when the data folder is on another disk, and a failure here stops the run with nothing deleted.
function Save-KeptData([string]$keep) {
    if (-not (Test-Path -LiteralPath $Data)) { return }
    New-Item -ItemType Directory -Force -Path $keep | Out-Null
    foreach ($name in $KeepItems) {
        $item = Join-Path $Data $name
        if (-not (Test-Path -LiteralPath $item)) { continue }
        try { Copy-Item -LiteralPath $item -Destination (Join-Path $keep $name) -Recurse -Force }
        catch { Fail ("could not save " + $item + ": " + $_.Exception.Message + "; nothing was deleted") }
    }
    Emit ("RESULT:kept=" + $keep)
}

# Put the kept items back into the new data folder. Hokma is not running yet (install.ps1 ran with -NoDoctor), so
# the database the install may have created is simply replaced; the next start migrates the old one.
function Restore-KeptData([string]$keep) {
    if (-not (Test-Path -LiteralPath $keep)) { return }
    $ok = $true
    if (Test-Path -LiteralPath (Join-Path $keep "hokma.db")) {
        foreach ($name in @("hokma.db", "hokma.db-wal", "hokma.db-shm")) {
            Remove-Item -LiteralPath (Join-Path $Data $name) -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($name in $KeepItems) {
        $item = Join-Path $keep $name
        if (-not (Test-Path -LiteralPath $item)) { continue }
        $target = Join-Path $Data $name
        try {
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
            Copy-Item -LiteralPath $item -Destination $target -Recurse -Force
        }
        catch { Write-Log ("restore " + $name + ": " + $_.Exception.Message); $ok = $false }
    }
    if ($ok) {
        Remove-Item -LiteralPath $keep -Recurse -Force -ErrorAction SilentlyContinue
        Emit "RESULT:plugins=restored"
    }
    else { Emit ("RESULT:kept_failed=" + $keep) }
}

function Install-Git {
    if (Test-Git) { Emit "RESULT:git=present"; return }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $code = Invoke-Logged "winget" @("install", "--id", "Git.Git", "-e", "--scope", "user", "--silent",
                                         "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity")
        Write-Log ("winget exit code " + $code)
        if (Test-Git) { Emit "RESULT:git=winget"; return }
    }
    # Fallback: the official installer pinned in lock.json, verified by sha256 before it runs.
    $lock = Get-Content -LiteralPath (Join-Path $SrcDir "installer\lock.json") -Raw | ConvertFrom-Json
    $entry = $lock.git_for_windows.(Get-Platform)
    if (-not $entry) { Fail "lock.json has no git_for_windows entry for this platform" }
    $exe = Join-Path $WorkTmp "git-setup.exe"
    if (-not (Invoke-HokmaDownload $entry.url $exe "Git" { param($t) Emit ("LINE:" + $t) })) {
        Fail ("git download failed after 3 attempts: " + $entry.url + " (" + $script:DownloadError + ")")
    }
    $actual = Get-Sha256 $exe
    if ($actual -ne $entry.sha256.ToLower()) { Fail ("git sha256 mismatch: expected " + $entry.sha256 + ", got " + $actual) }
    $proc = Start-Process -FilePath $exe -ArgumentList "/VERYSILENT /NORESTART /CURRENTUSER /SUPPRESSMSGBOXES /NOCANCEL /SP-" -Wait -PassThru
    Write-Log ("git setup exit code " + $proc.ExitCode)
    if (-not (Test-Git)) { Fail "git could not be installed (winget and the official installer both failed)" }
    Emit "RESULT:git=installer"
}

# Claude Code is recommended, never required: a failure is reported and the install goes on.
function Install-Claude {
    if (Test-Claude) { Emit "RESULT:claude=present"; return }
    $script = Join-Path $WorkTmp "claude-install.ps1"
    try {
        if (-not (Invoke-HokmaDownload "https://claude.ai/install.ps1" $script "Claude Code" { param($t) Emit ("LINE:" + $t) })) {
            throw ("download failed after 3 attempts: " + $script:DownloadError)
        }
        $code = Invoke-Logged "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $script)
        Write-Log ("claude install exit code " + $code)
    }
    catch { Write-Log ("claude install: " + $_.Exception.Message) }
    if (Test-Claude) { Emit "RESULT:claude=installed" } else { Emit "RESULT:claude=failed" }
}

# Put the student's folder back where it was: the fresh folder install.ps1 made is replaced by the backup, then
# kit-sync adds the new kit files (it never overwrites a file of the student).
function Restore-WorkFolder([string]$workdir, [string]$backup) {
    if (-not $backup) { return }
    $script:PendingBackup = ""
    $fresh = $workdir.TrimEnd("\") + "-new-" + (Get-Date -Format "yyyyMMddHHmmss")
    try {
        if (Test-Path -LiteralPath $workdir) { [System.IO.Directory]::Move($workdir, $fresh) }
        [System.IO.Directory]::Move($backup, $workdir)
    }
    catch {
        Write-Log ("restore failed: " + $_.Exception.Message)
        if ((-not (Test-Path -LiteralPath $workdir)) -and (Test-Path -LiteralPath $fresh)) { [System.IO.Directory]::Move($fresh, $workdir) }
        Emit ("RESULT:restore=failed")
        return
    }
    Remove-Item -LiteralPath $fresh -Recurse -Force -ErrorAction SilentlyContinue
    $code = Invoke-Logged "cmd.exe" @("/d", "/c", ('"' + $Launcher + '" kit-sync'))
    Write-Log ("kit-sync exit code " + $code)
    Emit "RESULT:restore=done"
}

function Install-WebView2 {
    $key = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    foreach ($path in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$key",
                        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$key",
                        "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$key")) {
        $pv = (Get-ItemProperty -Path $path -Name pv -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne "0.0.0.0") { Emit "RESULT:webview2=present"; return }
    }
    if (-not $WebView2 -or -not (Test-Path -LiteralPath $WebView2)) { Emit "RESULT:webview2=missing"; return }
    $proc = Start-Process -FilePath $WebView2 -ArgumentList "/silent /install" -Wait -PassThru
    Write-Log ("webview2 bootstrapper exit code " + $proc.ExitCode)
    Emit ("RESULT:webview2=" + $(if ($proc.ExitCode -eq 0) { "installed" } else { "failed" }))
}

# Leftovers of earlier runs in %LOCALAPPDATA%\Hokma-Setup: work folders of a run that crashed (older than a day)
# and logs older than 30 days. Folders with kept plugins and database (kept-*) are never removed here.
function Clear-OldSetupFiles {
    if (-not (Test-Path -LiteralPath $SetupHome)) { return }
    $now = Get-Date
    Get-ChildItem -LiteralPath $SetupHome -Directory -Filter "tmp-*" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $now.AddDays(-1) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    Get-ChildItem -LiteralPath $SetupHome -File -Filter "setup-*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $now.AddDays(-30) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

# ---- actions ---------------------------------------------------------------------------------------------------

if ($Log) { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null }
Write-Log ("action " + $Action + " mode " + $Mode + " dir " + $Dir)

switch ($Action) {
    "Detect" {
        # key=value lines, UTF-8 with BOM so the setup reads accents right.
        $lines = @("hokma=1")
        $found = (Test-Path -LiteralPath $Launcher) -or (Test-Path -LiteralPath (Join-Path $Data "current"))
        $lines += "found=" + $(if ($found) { "1" } else { "0" })
        $lines += "workdir=" + (Get-WorkFolder)
        $lines += "git=" + $(if (Test-Git) { "1" } else { "0" })
        $lines += "claude=" + $(if (Test-Claude) { "1" } else { "0" })
        $version = ""
        if (Test-Path -LiteralPath $Launcher) {
            $info = New-Object System.Diagnostics.ProcessStartInfo("cmd.exe", ('/d /c ""' + $Launcher + '" version"'))
            $info.UseShellExecute = $false; $info.RedirectStandardOutput = $true; $info.CreateNoWindow = $true
            try {
                $proc = [System.Diagnostics.Process]::Start($info)
                $read = $proc.StandardOutput.ReadToEndAsync()
                if ($proc.WaitForExit(10000)) { $version = ($read.Result -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1) }
                else { try { $proc.Kill() } catch { } }
            }
            catch { }
        }
        $lines += "version=" + ("" + $version).Trim()
        [System.IO.File]::WriteAllLines($Out, [string[]]$lines, (New-Object System.Text.UTF8Encoding($true)))
        exit 0
    }
    "WaitPid" { Wait-ForPid $WaitFor; exit 0 }
    "Stop" { Stop-Hokma; exit 0 }
    "Finish" {
        Emit "STAGE:shortcuts"
        if (Test-Path -LiteralPath $Launcher) {
            $code = Invoke-Logged "cmd.exe" @("/d", "/c", ('"' + $Launcher + '" shortcuts --force'))
            Write-Log ("shortcuts exit code " + $code)
        }
        if (-not $NoApp) {
            Emit "STAGE:webview"
            Install-WebView2
        }
        exit 0
    }
}

# ---- Run: every stage in order -----------------------------------------------------------------------------------
if (-not $Dir) { Fail "no working folder given" }
if (-not (Test-Path -LiteralPath $Src)) { Fail ("source package not found: " + $Src) }
$Dir = [System.IO.Path]::GetFullPath($Dir)
Clear-OldSetupFiles
$WorkTmp = Join-Path $env:LOCALAPPDATA ("Hokma-Setup\tmp-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $WorkTmp | Out-Null
$SrcDir = Join-Path $WorkTmp "src"
$script:PendingBackup = ""
try {
    if ($WaitFor -gt 0) { Wait-ForPid $WaitFor }

    Emit "STAGE:closing"
    Stop-Hokma
    if ($Mode -eq "update") { Remove-StaleLock }

    Expand-Archive -LiteralPath $Src -DestinationPath $SrcDir -Force
    . (Join-Path $SrcDir "installer\download.ps1")
    if (-not (Test-Path -LiteralPath (Join-Path $SrcDir "installer\install.ps1"))) { Fail "the source package has no installer\install.ps1" }

    $backup = ""
    $keep = Join-Path $SetupHome ("kept-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    if ($Mode -eq "reinstall") {
        Emit "STAGE:backup"
        Save-KeptData $keep
        $backup = Backup-WorkFolder $Dir
        Emit "STAGE:reset"
        $code = Invoke-Logged "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                                 (Join-Path $SrcDir "installer\uninstall.ps1"), "-ResetAll", "-Dir", $Dir, "-Lang", $Lang)
        if ($code -ne 0 -or (Test-Path -LiteralPath $Data)) { Fail ("reset failed (exit code " + $code + ")") }
    }

    Emit "STAGE:git"
    Install-Git

    if ($InstallClaude) {
        Emit "STAGE:claude"
        Install-Claude
    }

    Emit "STAGE:install"
    $code = Invoke-Logged "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                             (Join-Path $SrcDir "installer\install.ps1"), "-Dir", $Dir, "-Lang", $Lang, "-NoDoctor")
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $Launcher)) { Fail ("install.ps1 failed (exit code " + $code + ")") }

    if ($Mode -eq "reinstall") {
        Emit "STAGE:restore"
        Restore-KeptData $keep
        Restore-WorkFolder $Dir $backup
    }
    Emit "RESULT:done=1"
    exit 0
}
catch {
    Fail $_.Exception.Message
}
finally {
    # A run that stopped after the working folder was renamed puts it back, so the student never loses its name.
    if ($script:PendingBackup -and (Test-Path -LiteralPath $script:PendingBackup)) {
        try {
            if (Test-Path -LiteralPath $Dir) {
                [System.IO.Directory]::Move($Dir, ($Dir.TrimEnd("\") + "-failed-" + (Get-Date -Format "yyyyMMddHHmmss")))
            }
            [System.IO.Directory]::Move($script:PendingBackup, $Dir)
            Write-Log ("working folder put back after the failure: " + $Dir)
        }
        catch { Write-Log ("could not put the working folder back: " + $_.Exception.Message) }
    }
    Remove-Item -LiteralPath $WorkTmp -Recurse -Force -ErrorAction SilentlyContinue
}
