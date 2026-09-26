' Undermind Supervisor - mid-session crash watchdog (hidden at login)
' Complements gateway-service\Undermind_Proxy.vbs: that one boots the
' proxy at sign-in, this loop revives it if it dies while working.
' The revived proxy self-guards (quiet exit if the port is taken).
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\dewayne\Downloads\undermind"
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\dewayne\Downloads\undermind\scripts\undermind_supervisor.ps1""", 0, False
