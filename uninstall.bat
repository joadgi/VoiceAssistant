@echo off
cd /d "%~dp0"

echo ============================================
echo  Voice Assistant - Uninstall
echo ============================================
echo.
echo This app installs nothing into Program Files and changes no system
echo settings. Everything it creates lives in one of four places:
echo.
echo   1. A Windows startup entry (only if you enabled "start with Windows")
echo   2. A desktop shortcut     (only if you ran create_shortcut.bat)
echo   3. This folder            (venv, settings, logs)
echo   4. The Whisper model cache in your user profile (can be several GB)
echo.
echo Steps 1-3 run now. Step 4 asks first, because that model cache is
echo SHARED with any other AI tool on this machine that uses Hugging Face.
echo.
pause
echo.

rem ----------------------------------------------------------------- step 1
echo [1/4] Removing the "start with Windows" entry...
set "RUNKEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
reg query "%RUNKEY%" /v VoiceAssistant >nul 2>&1
if errorlevel 1 goto :s1none
reg delete "%RUNKEY%" /v VoiceAssistant /f >nul 2>&1
reg query "%RUNKEY%" /v VoiceAssistant >nul 2>&1
if errorlevel 1 goto :s1done
echo       COULD NOT REMOVE IT. Open Task Manager, Startup tab, and
echo       disable "VoiceAssistant" by hand.
goto :step2
:s1none
echo       Not present - nothing to do.
goto :step2
:s1done
echo       Removed.

rem ----------------------------------------------------------------- step 2
:step2
echo.
echo [2/4] Removing the desktop shortcut...
set "LNK=%USERPROFILE%\Desktop\Voice Assistant.lnk"
if not exist "%LNK%" goto :s2none
del /f /q "%LNK%" >nul 2>&1
if exist "%LNK%" goto :s2fail
echo       Removed.
goto :step3
:s2none
echo       Not present - nothing to do.
goto :step3
:s2fail
echo       COULD NOT REMOVE IT. Delete this file by hand:
echo       %LNK%

rem ----------------------------------------------------------------- step 3
:step3
echo.
echo [3/4] Removing the local environment and runtime files...
echo.
set "DROPCFG="
set "KEEPCFG="
set /p "KEEPCFG=      Keep your settings.json (hotkeys, voice, model choice)? [Y/n] "
if /i "%KEEPCFG%"=="n" set "DROPCFG=1"
echo.

call :killdir "venv"
call :killdir "__pycache__"
call :killdir ".pytest_cache"
for /d /r %%D in (__pycache__) do call :killdir "%%D"
call :killfile "debug.log"
call :killfile "crash.log"
call :killfile "metrics.jsonl"
call :killfile "metrics.jsonl.1"
if defined DROPCFG call :killfile "settings.json"
if not defined DROPCFG call :keepfile "settings.json"
echo       Done.

rem ----------------------------------------------------------------- step 4
echo.
echo [4/4] Whisper speech models (downloaded on first run).
echo.
if defined HF_HOME set "HFHUB=%HF_HOME%\hub"
if not defined HF_HOME set "HFHUB=%USERPROFILE%\.cache\huggingface\hub"
if not exist "%HFHUB%" goto :nocache

set "FOUND="
for /d %%M in ("%HFHUB%\models--Systran--faster-whisper-*") do call :listmodel "%%~nxM"
if not defined FOUND goto :nomodels

echo.
echo       Those are Whisper speech models this app downloaded. Deleting
echo       them frees several GB. Any OTHER Hugging Face models in that
echo       cache belong to other tools and will NOT be touched.
echo.
set "DELMODELS="
set /p "DELMODELS=      Delete the faster-whisper models? [y/N] "
if /i not "%DELMODELS%"=="y" goto :keptmodels
for /d %%M in ("%HFHUB%\models--Systran--faster-whisper-*") do call :killmodel "%%M" "%%~nxM"
echo       Done.
goto :done

:keptmodels
echo       Kept. To remove them later, delete those folders from:
echo       %HFHUB%
goto :done

:nocache
echo       No model cache found - nothing to do.
goto :done

:nomodels
echo       No faster-whisper models in the cache - nothing to do.
goto :done

rem ----------------------------------------------------------------- report
:done
echo.
echo ============================================
echo  Uninstall complete
echo ============================================
echo.
echo The startup entry, desktop shortcut, virtual environment and local
echo logs are gone. This app never wrote to Program Files, nor to the
echo registry outside that one HKCU startup value.
echo.
echo ONE MANUAL STEP REMAINS: this script is running from inside the
echo folder, so it cannot delete the folder itself. Close this window,
echo then delete:
echo.
echo   %~dp0
echo.
echo NOT removed, because this app did not install them: Python and VLC.
echo Both are general-purpose software you may want for other reasons.
echo Remove them from Settings ^> Apps only if you no longer need them.
echo.
pause
goto :eof

rem --------------------------------------------------------------- helpers
:killdir
if not exist %~1 goto :eof
echo       removing %~1
rmdir /s /q %~1 2>nul
goto :eof

:killfile
if not exist %~1 goto :eof
echo       removing %~1
del /f /q %~1 2>nul
goto :eof

:keepfile
if not exist %~1 goto :eof
echo       keeping  %~1
goto :eof

:listmodel
set "FOUND=1"
echo       found: %~1
goto :eof

:killmodel
echo       deleting %~2 ...
rmdir /s /q %~1 2>nul
goto :eof
