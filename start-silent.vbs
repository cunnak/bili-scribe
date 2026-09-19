' Launch bili-scribe without a console window.
'
' First run is different: if there is no .venv yet, start.bat needs to create
' one and install dependencies, which takes a while and can fail. That must be
' visible, otherwise a failed first run looks like "nothing happened".
Option Explicit

Dim shell, fso, here, bat, mode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
bat = here & "\start.bat"

If Not fso.FileExists(bat) Then
  MsgBox "start.bat was not found next to this script." & vbCrLf & _
         "Expected location:" & vbCrLf & bat, 16, "bili-scribe"
  WScript.Quit 1
End If

If fso.FileExists(here & "\.venv\Scripts\python.exe") Then
  mode = 0   ' already set up - stay out of the way
Else
  mode = 1   ' first run - show the console so setup is visible
End If

shell.CurrentDirectory = here
shell.Run """" & bat & """", mode, False
