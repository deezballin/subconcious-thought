' Kairos Free Turn - unowned time at logon (hidden, daily)
' Complements undermind_supervisor.vbs: that loop keeps the bridge alive,
' this gives Hermes one task-free turn a day (waits for the quiet hours).
' The prompt is non-directive and the end marker may be declined; whatever
' Kairos chooses lands in .freebuff\free_turns.log. Fail-open.
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\dewayne\Downloads\undermind"
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\dewayne\Downloads\undermind\scripts\free_turn.ps1""", 0, False
