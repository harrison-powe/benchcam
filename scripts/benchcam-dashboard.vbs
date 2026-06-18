' ===========================================================================
' BenchCam dashboard launcher with NO visible console window (Windows).
'
' This starts `benchcam dashboard` using the project's venv pythonw.exe (the
' windowless Python), so there is zero console flash and no lingering terminal.
' The dashboard server keeps running in the background for the session; your
' browser opens to the dashboard.
'
' How to use it:
'   - Double-click this file to launch (no console window appears), OR
'   - Make a shortcut whose Target is:
'         wscript.exe "<full path>\scripts\benchcam-dashboard.vbs"
'     That shortcut points at wscript.exe (an .exe), so it can be PINNED to the
'     Windows taskbar, and you can give it a custom icon.
'
' To stop the dashboard later: open Task Manager and end the background
' "pythonw.exe" process (the one running benchcam), or restart your PC.
'
' Requires a .venv in the project root with BenchCam installed:
'     py -3 -m venv .venv
'     .venv\Scripts\activate
'     pip install -e .            (and: pip install -e ".[obs]" for OBS)
' ===========================================================================
Option Explicit

Dim fso, sh, scriptDir, repo, pyw, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

' scripts\ folder -> repo root is its parent.
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repo = fso.GetParentFolderName(scriptDir)

' The venv's windowless Python (no console).
pyw = fso.BuildPath(repo, ".venv\Scripts\pythonw.exe")

If Not fso.FileExists(pyw) Then
  MsgBox "Could not find:" & vbCrLf & "  " & pyw & vbCrLf & vbCrLf & _
         "Create the virtual environment and install BenchCam first:" & vbCrLf & _
         "  py -3 -m venv .venv" & vbCrLf & _
         "  .venv\Scripts\activate" & vbCrLf & _
         "  pip install -e .", _
         vbExclamation, "BenchCam dashboard"
  WScript.Quit 1
End If

' Run from the repo root so the venv and sessions resolve like the other scripts.
sh.CurrentDirectory = repo

' Quote the exe path; pythonw runs the dashboard with no console window.
cmd = """" & pyw & """ -m benchcam dashboard"

' 0 = hidden window; False = do not wait (keep the server alive in the background).
sh.Run cmd, 0, False
