' Launch tick.cmd with no visible console window.
'
' Why this exists: a scheduled task running a .cmd shows a console window every
' time it fires. Every five minutes, all day, that is unusable. The proper fix is
' the S4U logon type ("run whether the user is logged on or not"), but setting it
' requires administrator rights, which a normal user account does not have.
'
' WScript.Shell.Run with intWindowStyle 0 starts the process hidden, and
' bWaitOnReturn False lets this launcher exit immediately so Task Scheduler is not
' left holding a handle. Output still goes to logs\task-out.log via tick.cmd.
'
' Point the scheduled task at:  wscript.exe "<repo>\scripts\tick-hidden.vbs"

Option Explicit

Dim shell, scriptDir, target

Set shell = CreateObject("WScript.Shell")

' Folder containing this script, with its trailing backslash.
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
target = scriptDir & "tick.cmd"

' 0 = hidden window, False = do not wait for it to finish.
shell.Run """" & target & """", 0, False
