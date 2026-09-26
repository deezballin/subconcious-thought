#
# undermind_proxy_watchdog.ps1 — keep the Undermind proxy alive.
#
# Invoked by the "UndermindProxyWatchdog" scheduled task every 5 minutes
# and at logon. Idempotent by design: `undermind --proxy` (run_forever)
# exits 0 quietly when :11435 is already serving, so re-launching is
# always safe. Logs append to .freebuff/watchdog.log in the repo.
#
$Repo = "C:\Users\dewayne\Downloads\undermind"
$Uv   = "C:\Users\dewayne\AppData\Local\hermes\bin\uv.exe"
$Log  = Join-Path $Repo ".freebuff\watchdog.log"

if (-not (Test-Path (Join-Path $Repo ".freebuff"))) {
    New-Item -ItemType Directory -Path (Join-Path $Repo ".freebuff") | Out-Null
}

function Log($msg) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $Log -Value "$stamp $msg"
}

Log "watchdog tick: launching proxy guard"
& $Uv run --project $Repo python -m undermind.main --proxy *>> $Log
Log "watchdog tick done (exit $LASTEXITCODE)"
