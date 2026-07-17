@echo off
echo ============================================
echo  Voice Assistant - First Time Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Create virtual environment
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

:: Activate and install
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Installing dependencies...
echo (OCR uses the Windows-native engine - no PyTorch download needed.)
pip install -r requirements.txt

echo.
echo ============================================
echo  Setup complete!
echo  Run 'run.bat' to start the assistant.
echo ============================================
echo.
echo NOTE: First launch downloads the Whisper speech model (one-time).
echo       NVIDIA GPU is used automatically when present; otherwise CPU.
echo       Neural read-aloud voices need VLC: winget install VideoLAN.VLC
echo.
pause
