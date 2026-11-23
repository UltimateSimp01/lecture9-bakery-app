Option Explicit

Dim files, hostShell, folder, program
Set files = CreateObject("Scripting.FileSystemObject")
Set hostShell = CreateObject("WScript.Shell")
folder = files.GetParentFolderName(WScript.ScriptFullName)
program = files.BuildPath(folder, "app.py")

If Not files.FileExists(program) Then
    MsgBox "Extract the whole bakery folder before opening the order desk. Keep app.py beside this launcher.", vbExclamation, "Bakery Order Desk"
    WScript.Quit 1
End If

hostShell.CurrentDirectory = folder
If StartPython("pyw.exe -3") Then WScript.Quit 0
If StartPython("pythonw.exe") Then WScript.Quit 0
If StartPython("py.exe -3") Then WScript.Quit 0
If StartPython("python.exe") Then WScript.Quit 0

MsgBox "The bakery order desk needs Python 3 on this computer. Open it on a course computer with Python available.", vbExclamation, "Bakery Order Desk"
WScript.Quit 1

Function StartPython(ByVal command)
    Dim arguments
    arguments = " -B " & Chr(34) & program & Chr(34) & " --open"
    On Error Resume Next
    hostShell.Run command & arguments, 0, False
    StartPython = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
End Function
