' ============================================================
'  Atlas Console - silent launcher (no console window)
'  Double-click to start. Close the app window to stop the backend.
' ============================================================
Dim fso, sh, dir, exe
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
exe = dir & "\node_modules\electron\dist\electron.exe"

If Not fso.FileExists(exe) Then
  MsgBox "Electron is not installed yet." & vbCrLf & vbCrLf & _
         "Open a terminal in this folder and run:  npm install", _
         48, "Atlas Console"
  WScript.Quit 1
End If

sh.CurrentDirectory = dir

' Run with a scrubbed environment: strip vars that break Electron
' when the launcher itself was started from an Electron-hosted shell.
Dim envCmd
envCmd = "cmd /c set ELECTRON_RUN_AS_NODE=&& set NODE_OPTIONS=&& """ & exe & """ ."
sh.Run envCmd, 0, False
