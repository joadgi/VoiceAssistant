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
echo Installing PyTorch with CUDA support...
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

echo.
echo Installing dependencies...
pip install -r requirements.txt

echo.
echo ============================================
echo  Setup complete!
echo  Run 'run.bat' to start the assistant.
echo ============================================
echo.
echo NOTE: First launch will download Whisper and OCR models.
echo       This is a one-time ~1GB download.
echo.
pause
