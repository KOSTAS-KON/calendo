' Creates a proper Desktop shortcut (.lnk) to open TherapySMS landing page.
' Uses Explorer directly (avoids cmd/start quoting edge-cases).

Option Explicit

Dim shell, desktop, lnkPath, link, explorerExe, iconLocation

Set shell = CreateObject("WScript.Shell")
desktop = shell.SpecialFolders("Desktop")
lnkPath = desktop & "\\TherapySMS.lnk"

explorerExe = shell.ExpandEnvironmentStrings("%SystemRoot%\\explorer.exe")
iconLocation = shell.ExpandEnvironmentStrings("%SystemRoot%\\System32\\SHELL32.dll") & ",220"

Set link = shell.CreateShortcut(lnkPath)
link.TargetPath = explorerExe
link.Arguments = "http://localhost:8080/"
link.WorkingDirectory = shell.CurrentDirectory
link.WindowStyle = 1
link.IconLocation = iconLocation
link.Description = "Open TherapySMS"
link.Save
