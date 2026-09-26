#
# undermind_supervisor.ps1 - mid-session crash supervisor (no admin needed).
#
# Loops forever: every 5 minutes, if nothing is listening on :11435,
# relaunch the proxy detached (hidden). Complements the logon launcher
# (gateway-service\Undermind_Proxy.vbs): that one boots the proxy at
# sign-in, this one revives it if it dies while you are working.
# The launched proxy self-guards (exits quietly if the port is taken),
# so double-launch races are harmless.
#
$Repo = "C:\Users\dewayne\Downloads\undermind"
$Uv   = "C:\Users\dewayne\AppData\Local\hermes\bin\uv.exe"
$Port = 11435
$EverySeconds = 300
$Log = Join-Path $Repo ".freebuff\supervisor.log"

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

Log "supervisor started (checking port $Port every $EverySeconds seconds)"

while ($true) {
    if (-not (Test-PortListening $Port)) {
        Log "port $Port dead - relaunching proxy"
        $startArgs = @{
            FilePath               = $Uv
            ArgumentList           = @("run", "--project", $Repo, "python", "-m", "undermind.main", "--proxy")
            WorkingDirectory       = $Repo
            WindowStyle            = "Hidden"
            RedirectStandardOutput = (Join-Path $Repo ".freebuff\proxy_stdout.log")
            RedirectStandardError  = (Join-Path $Repo ".freebuff\proxy_stderr.log")
        }
        Start-Process @startArgs
        Start-Sleep -Seconds 20
        if (Test-PortListening $Port) {
            Log "relaunch OK - proxy is serving"
        } else {
            Log "relaunch did not bind yet (will retry next tick)"
        }
    }
    Start-Sleep -Seconds $EverySeconds
}
