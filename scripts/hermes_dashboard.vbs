' Hermes Dashboard - web UI autostart (hidden at login)
' Complements gateway-service\Undermind_Proxy.vbs and undermind_supervisor.vbs:
' the gateway daemons boot elsewhere, this brings up the web dashboard on
' :9119. The launcher self-guards (quiet exit when the port already serves),
' so double-launch races are harmless.
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\dewayne\Downloads\undermind"
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\dewayne\Downloads\undermind\scripts\hermes_dashboard.ps1""", 0, False
