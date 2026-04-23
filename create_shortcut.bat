@echo off
:: Creates a desktop shortcut for Voice Assistant
set SCRIPT_DIR=%~dp0
set SHORTCUT="%USERPROFILE%\Desktop\Voice Assistant.lnk"
set TARGET="%SCRIPT_DIR%run.bat"

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut(%SHORTCUT%); $sc.TargetPath = %TARGET%; $sc.WorkingDirectory = '%SCRIPT_DIR%'; $sc.Description = 'Voice Assistant - Local Dictation'; $sc.WindowStyle = 7; $sc.Save()"

echo.
echo Desktop shortcut created: Voice Assistant
echo.
pause
