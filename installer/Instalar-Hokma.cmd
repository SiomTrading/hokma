@echo off
rem Hokma one-file installer for Windows. Double click it: no commands to type.
rem It closes a running Hokma, downloads the main branch of SiomTrading/hokma to a temporary folder, runs
rem installer\install.ps1, creates the shortcuts (install.ps1 does it) and opens Hokma at the end.
rem The first lines are batch and end at the exit line; everything after the marker is PowerShell, read as UTF-8.
set "HOKMA_SELF=%~f0"
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$t=[IO.File]::ReadAllText($env:HOKMA_SELF,[Text.Encoding]::UTF8); Invoke-Expression $t.Substring($t.IndexOf('#'+'PSBEGIN'))"
exit /b 0

#PSBEGIN
# Student-facing text is Spanish on purpose: this file travels alone (the repository is not there yet), so it
# cannot read the message catalogs. scripts/check_repo_rules.py lists it as an allowed place for Spanish.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$RepoUrl = "https://github.com/SiomTrading/hokma.git"
$T = @{
    title        = "Instalar Hokma"
    no_git       = "Falta instalar git, y Hokma lo necesita.`r`n`r`nInstálalo desde https://git-scm.com/download/win (todo por defecto, Siguiente hasta el final) y vuelve a abrir este instalador."
    no_claude    = "Recomendamos Claude Code para chatear; puedes instalarlo después.`r`n`r`nCuando quieras, abre PowerShell, pega esta línea y presiona Enter:`r`n`r`nirm https://claude.ai/install.ps1 | iex"
    closing      = "Cerrando Hokma si está abierto..."
    downloading  = "Descargando la última versión de Hokma..."
    installing   = "Instalando Hokma. Puede tardar unos minutos..."
    opening      = "Abriendo Hokma..."
    dl_failed    = "No pude descargar Hokma. Revisa tu conexión a internet y vuelve a abrir este instalador.`r`n`r`nDetalle:"
    inst_failed  = "La instalación falló. Vuelve a abrir este instalador; si se repite, envía este mensaje a soporte.`r`n`r`nDetalle:"
    not_found    = "La instalación terminó pero no encuentro el comando de Hokma. Vuelve a abrir este instalador."
    log_at       = "Registro completo en:"
}

$Form = New-Object System.Windows.Forms.Form
$Form.Text = $T.title
$Form.Size = New-Object System.Drawing.Size(460, 130)
$Form.StartPosition = "CenterScreen"
$Form.FormBorderStyle = "FixedDialog"
$Form.MaximizeBox = $false
$Form.MinimizeBox = $false
$Form.ControlBox = $false
$Form.TopMost = $true
$Label = New-Object System.Windows.Forms.Label
$Label.Location = New-Object System.Drawing.Point(16, 14)
$Label.Size = New-Object System.Drawing.Size(420, 36)
$Bar = New-Object System.Windows.Forms.ProgressBar
$Bar.Style = "Marquee"
$Bar.Location = New-Object System.Drawing.Point(16, 56)
$Bar.Size = New-Object System.Drawing.Size(420, 18)
$Form.Controls.Add($Label)
$Form.Controls.Add($Bar)

function Pump { [System.Windows.Forms.Application]::DoEvents() }
function Show-Step([string]$text) { $Label.Text = $text; Pump }
function Show-Box([string]$text, [string]$icon) {
    $Form.TopMost = $false
    [void][System.Windows.Forms.MessageBox]::Show($text, $T.title, "OK", $icon)
}
function Fail([string]$text) { Show-Box $text "Error"; $Form.Close(); exit 1 }

# Run a program with no window, output to a log file, keeping the progress window alive. Returns the exit code.
function Invoke-Hidden([string]$file, [string]$argLine, [string]$log, [scriptblock]$onTick) {
    $p = Start-Process -FilePath $file -ArgumentList $argLine -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError ($log + ".err")
    $null = $p.Handle     # keeps the process handle so ExitCode is not $null on Windows PowerShell 5.1
    while (-not $p.HasExited) {
        Start-Sleep -Milliseconds 200
        Pump
        if ($onTick) { & $onTick }
    }
    $p.WaitForExit()
    return $p.ExitCode
}
# Success is decided by the exit code, never by what the program wrote to stderr (git prints "Cloning into" there).
# If Windows gives no exit code at all, the proof file decides.
function Test-Succeeded($code, [string]$proof) {
    if ($null -eq $code) { return (Test-Path -LiteralPath $proof) }
    return ([int]$code -eq 0)
}
function Read-Tail([string]$path, [int]$count) {
    if (-not (Test-Path -LiteralPath $path)) { return "" }
    $lines = @(Get-Content -LiteralPath $path -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_ -and $_.Trim() })
    if ($lines.Count -eq 0) { return "" }
    return (($lines | Select-Object -Last $count) -join "`r`n")
}

$Form.Show()
Show-Step $T.closing

# ---- 1. git must exist (it stops the install); claude is only a warning, shown at the end. Nothing is installed behind the student's back
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail $T.no_git }
$claudeFile = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
$claudeMissing = (-not (Get-Command claude -ErrorAction SilentlyContinue)) -and (-not (Test-Path -LiteralPath $claudeFile))

# ---- 2. close a running Hokma: new supervisor (hokma stop) or old engine in a console, only under <data>
$data = if ($env:HOKMA_DATA_DIR) { $env:HOKMA_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "Hokma" }
$data = [System.IO.Path]::GetFullPath($data)
$launcher = Join-Path $env:USERPROFILE ".local\bin\hokma.cmd"
if (Test-Path -LiteralPath $launcher) {
    $stop = Start-Process -FilePath "cmd.exe" -ArgumentList ('/d /c ""' + $launcher + '" stop"') -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    while (-not $stop.HasExited -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200; Pump }
    if (-not $stop.HasExited) { try { $stop.Kill() } catch { } }
}
$roots = @(
    ((Join-Path $data "runtime").TrimEnd("\") + "\"),
    ((Join-Path $data "python").TrimEnd("\") + "\")
)
function Test-UnderRuntime($proc) {
    foreach ($r in $roots) {
        if ($proc.ExecutablePath -and $proc.ExecutablePath.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($proc.CommandLine -and $proc.CommandLine.IndexOf($r, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    }
    return $false
}
foreach ($attempt in 1..2) {
    $left = @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and (Test-UnderRuntime $_) })
    foreach ($proc in $left) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($left.Count -gt 0) { Start-Sleep -Milliseconds 800; Pump }
}

# ---- 3. download main branch of SiomTrading/hokma to a temporary folder
$work = Join-Path $env:TEMP ("hokma-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$env:GIT_TERMINAL_PROMPT = "0"
try {
    Show-Step $T.downloading
    $src = Join-Path $work "src"
    $cloneLog = Join-Path $work "clone.log"
    $installScript = Join-Path $src "installer\install.ps1"
    $code = Invoke-Hidden "git" ('-c core.longpaths=true clone --quiet --depth 1 --branch main "' + $RepoUrl + '" "' + $src + '"') $cloneLog $null
    if (-not (Test-Succeeded $code $installScript)) {
        $detail = (Read-Tail ($cloneLog + ".err") 6) + "`r`n" + (Read-Tail $cloneLog 4)
        Fail ($T.dl_failed + "`r`n" + $detail.Trim() + "`r`n(git: " + $code + ")")
    }

    # ---- 4. the real installer
    Show-Step $T.installing
    $installLog = Join-Path $work "install.log"
    $tick = {
        $last = Read-Tail $installLog 1
        if ($last) { $Label.Text = ($last -replace "^\[[^\]]*\]\s*", ""); Pump }
    }
    Remove-Item Env:PSModulePath -ErrorAction SilentlyContinue
    $code = Invoke-Hidden "powershell.exe" ('-NoProfile -ExecutionPolicy Bypass -File "' + $installScript + '" -NoDoctor') $installLog $tick
    if (-not (Test-Succeeded $code $launcher)) {
        $keep = Join-Path $env:TEMP "hokma-install-last.log"
        Copy-Item -LiteralPath $installLog -Destination $keep -Force -ErrorAction SilentlyContinue
        Fail ($T.inst_failed + "`r`n" + (Read-Tail $installLog 6) + "`r`n" + (Read-Tail ($installLog + ".err") 4) + "`r`n`r`n" + $T.log_at + " " + $keep)
    }
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- 5. open Hokma (the shortcuts were created by install.ps1)
Show-Step $T.opening
if (-not (Test-Path -LiteralPath $launcher)) { Fail $T.not_found }
Start-Process -FilePath "cmd.exe" -ArgumentList ('/d /c ""' + $launcher + '" start"') -WindowStyle Hidden
Start-Sleep -Seconds 2
$Form.Close()
if ($claudeMissing) { Show-Box $T.no_claude "Information" }
exit 0
