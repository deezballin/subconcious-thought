#
# hermes_dashboard.ps1 - ensure the Hermes web dashboard is running (no admin).
#
# Idempotent: if something already listens on :9119 this exits quietly, so it
# is safe at every logon and safe to run manually anytime. Otherwise it
# launches "hermes dashboard --no-open" hidden and verifies it came up.
# Logs to .freebuff\dashboard.log.
#
$Repo   = "C:\Users\dewayne\Downloads\undermind"
$Hermes = "C:\Users\dewayne\AppData\Local\hermes\bin\hermes.exe"
$Port   = 9119
$Log    = Join-Path $Repo ".freebuff\dashboard.log"

function Test-PortListening([int]$p) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("127.0.0.1", $p, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(500)) { return $client.Connected }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Log($msg) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $Log -Value "$stamp $msg"
}

if (Test-PortListening $Port) {
    Log "dashboard already serving on $Port - nothing to do"
    exit 0
}

if (-not (Test-Path $Hermes)) {
    $cmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($cmd) {
        $Hermes = $cmd.Source
    } else {
        Log "hermes executable not found - cannot start dashboard"
        exit 1
    }
}

Log "dashboard not running - launching (hidden)"
Start-Process -FilePath $Hermes -ArgumentList "dashboard", "--no-open" -WindowStyle Hidden
Start-Sleep -Seconds 20
if (Test-PortListening $Port) {
    Log "dashboard OK - serving on $Port"
} else {
    Log "dashboard did not bind yet (check hermes logs; will retry next logon/tick)"
}
