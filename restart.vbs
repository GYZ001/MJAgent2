' 无黑窗后台重启漫剧 Agent 前后端（后端稳定模式，前端热刷新）
Option Explicit
Dim sh, root, py, cmd, rc
Set sh = CreateObject("WScript.Shell")
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
py = root & "\.venv\Scripts\python.exe"
If Not CreateObject("Scripting.FileSystemObject").FileExists(py) Then
  MsgBox "找不到 .venv\Scripts\python.exe", vbCritical, "MJAgent2"
  WScript.Quit 1
End If
' 0 = 隐藏窗口；True = 等完成（就绪检测）
cmd = """" & py & """ """ & root & "\scripts\dev_win.py"" restart"
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
