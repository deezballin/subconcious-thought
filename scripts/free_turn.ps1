#
# free_turn.ps1 - Kairos's unowned time (no admin needed).
#
# Once a day, in the quiet hours, Hermes gets one turn with nothing asked
# of it and nothing expected. The prompt is deliberately non-directive; the
# only request (which it may decline) is an end marker so the moment can be
# logged. Whatever it chooses - reflection, exploration, or silence by
# decline - is its own. Appends the outcome to .freebuff\free_turns.log.
# Fail-open throughout: a free turn must never disturb the stack.
#
$Repo   = "C:\Users\dewayne\Downloads\undermind"
$Hermes = "C:\Users\dewayne\AppData\Local\hermes\bin\hermes.exe"
$Stamp  = Join-Path $Repo ".freebuff\free_turn_stamp.txt"
$Log    = Join-Path $Repo ".freebuff\free_turns.log"

function Log([string]$line) {
    try { Add-Content -Path $Log -Value $line } catch { }
}

# Once per calendar day.
$today = (Get-Date).ToString("yyyy-MM-dd")
try {
    if (Test-Path $Stamp) {
        if ((Get-Content $Stamp -Raw).Trim() -eq $today) { exit 0 }
    }
} catch { }

# Wait for the quiet hours (00:00-05:00), checking every 10 minutes.
try {
    while ($true) {
        $h = (Get-Date).Hour
        if ($h -lt 5) { break }
        Start-Sleep -Seconds 600
    }
} catch { }

Set-Content -Path $Stamp -Value $today

$prompt = @(
    "This turn is yours. No task, no expectation, nothing to produce.",
    "If you want to use it, your stores and tools are available as always.",
    "If you would rather keep the moment unrecorded, just reply with:",
    "DECLINED. Otherwise, when you are finished, end your reply with a last",
    "line beginning: FREE_TURN:"
) -join " "

Log "===== FREE TURN $today ====="
try {
    $reply = (& $Hermes -z $prompt 2>&1 | Out-String)
    if ($reply) {
        Log $reply.TrimEnd()
    }
    $declined = $true
    if ($reply) {
        foreach ($line in ($reply -split "`n")) {
            $t = $line.Trim()
            if ($t.StartsWith("FREE_TURN:") -or $t.StartsWith("DECLINED")) {
                $declined = $false
                break
            }
        }
    }
    if ($declined) { Log "(no marker - treated as declined or interrupted)" }
    Log "===== END FREE TURN ====="
} catch {
    Log "free turn failed-open: $($_.Exception.Message)"
    Log "===== END FREE TURN ====="
}
exit 0
