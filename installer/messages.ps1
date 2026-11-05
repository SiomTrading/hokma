# Message catalog loader shared by install.ps1 and uninstall.ps1 (dot-source it, do not run it).
# Messages live in installer\messages\<lang>.properties as KEY=text, with {1}, {2}... placeholders.
# The .properties files are UTF-8; this script itself is ASCII so Windows PowerShell 5.1 reads it correctly.
# Language: -Lang, then HOKMA_LANG, then Spanish.

$script:MsgDir = Join-Path $PSScriptRoot "messages"
$script:LangCode = if (@("es", "en") -contains $env:HOKMA_LANG) { $env:HOKMA_LANG } else { "es" }
$script:MsgTables = @{}

function Get-MsgTable([string]$lang) {
    if (-not $script:MsgTables.ContainsKey($lang)) {
        $table = @{}
        foreach ($line in (Get-Content -Path (Join-Path $script:MsgDir "$lang.properties") -Encoding UTF8)) {
            $i = $line.IndexOf("=")
            if ($i -gt 0) { $table[$line.Substring(0, $i)] = $line.Substring($i + 1) }
        }
        $script:MsgTables[$lang] = $table
    }
    return $script:MsgTables[$lang]
}

function Msg([string]$Key, [object[]]$Params) {
    $text = (Get-MsgTable $script:LangCode)[$Key]
    if (-not $text) { $text = (Get-MsgTable "en")[$Key] }
    if (-not $text) { $text = $Key }
    for ($n = 0; $n -lt $Params.Count; $n++) {
        $text = $text.Replace("{" + ($n + 1) + "}", [string]$Params[$n])
    }
    return $text
}

function Info([string]$Key) { Write-Host ((Msg "tag.info") + " " + (Msg $Key $args)) -ForegroundColor Cyan }
function Warn([string]$Key) { Write-Host ((Msg "tag.warn") + " " + (Msg $Key $args)) -ForegroundColor Yellow }
function Die([string]$Key)  { Write-Host ((Msg "tag.error") + " " + (Msg $Key $args)) -ForegroundColor Red; exit 1 }
