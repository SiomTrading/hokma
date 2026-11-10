# Download helper shared by install.ps1 and setup-helper.ps1 (dot-source it, do not run it).
# Forces TLS 1.2, has a connect timeout and a no-data timeout (a slow download that keeps receiving bytes is never
# cut by a total time limit), retries 3 times with a wait, and reports the progress in MB every few seconds.
# This file is ASCII on purpose (Windows PowerShell 5.1 reads BOM-less files as ANSI).

# -Report is a script block that receives one text line (the caller prints it as a progress line).
# Returns $true when the file is complete, $false when every attempt failed (the last reason is in $script:DownloadError).
function Invoke-HokmaDownload([string]$Uri, [string]$OutFile, [string]$Label, [scriptblock]$Report,
                              [int]$Retries = 3, [int]$ConnectSeconds = 30, [int]$StallSeconds = 45, [int]$WaitSeconds = 4) {
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 }
    catch { }
    $script:DownloadError = ""
    for ($attempt = 1; $attempt -le $Retries; $attempt++) {
        $stream = $null; $file = $null; $response = $null
        try {
            $request = [Net.HttpWebRequest]::Create($Uri)
            $request.Timeout = $ConnectSeconds * 1000
            $request.ReadWriteTimeout = $StallSeconds * 1000
            $request.UserAgent = "Hokma-Setup"
            $request.AllowAutoRedirect = $true
            $response = $request.GetResponse()
            $total = $response.ContentLength
            $stream = $response.GetResponseStream()
            $file = [System.IO.File]::Create($OutFile)
            $buffer = New-Object byte[] 65536
            $done = 0L
            $clock = [System.Diagnostics.Stopwatch]::StartNew()
            $lastReport = -3000
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $file.Write($buffer, 0, $read)
                $done += $read
                if (($clock.ElapsedMilliseconds - $lastReport) -ge 3000) {
                    $lastReport = $clock.ElapsedMilliseconds
                    & $Report (Format-DownloadProgress $Label $done $total)
                }
            }
            $file.Dispose(); $file = $null
            if (($total -gt 0) -and ($done -ne $total)) { throw ("incomplete download: " + $done + " of " + $total + " bytes") }
            & $Report (Format-DownloadProgress $Label $done $done)
            return $true
        }
        catch {
            $script:DownloadError = $_.Exception.Message
            & $Report ($Label + ": attempt " + $attempt + " of " + $Retries + " failed (" + $script:DownloadError + ")")
        }
        finally {
            if ($file) { $file.Dispose() }
            if ($stream) { $stream.Dispose() }
            if ($response) { $response.Close() }
        }
        if (Test-Path -LiteralPath $OutFile) { Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue }
        if ($attempt -lt $Retries) { Start-Sleep -Seconds ($WaitSeconds * $attempt) }
    }
    return $false
}

function Format-DownloadProgress([string]$Label, [long]$Done, [long]$Total) {
    $mb = [math]::Round($Done / 1MB, 1).ToString([Globalization.CultureInfo]::InvariantCulture)
    if ($Total -gt 0) {
        $all = [math]::Round($Total / 1MB, 1).ToString([Globalization.CultureInfo]::InvariantCulture)
        return ($Label + ": " + $mb + " / " + $all + " MB")
    }
    return ($Label + ": " + $mb + " MB")
}
