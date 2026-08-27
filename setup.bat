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
if %errorlevel% neq 0 (
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo Installing the local neural read-aloud voice...
if not exist "models\kokoro" mkdir "models\kokoro"
if not exist "models\kokoro\kokoro-v1.0.fp16.onnx" (
    curl.exe -L --fail --output "models\kokoro\kokoro-v1.0.fp16.onnx" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.fp16.onnx"
    if %errorlevel% neq 0 (
        echo ERROR: Local neural model download failed.
        pause
        exit /b 1
    )
)
if not exist "models\kokoro\voices-v1.0.bin" (
    curl.exe -L --fail --output "models\kokoro\voices-v1.0.bin" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin"
    if %errorlevel% neq 0 (
        echo ERROR: Local neural voice download failed.
        pause
        exit /b 1
    )
)

echo.
echo ============================================
echo  Setup complete!
echo  Run 'run.bat' to start the assistant.
echo ============================================
echo.
echo NOTE: First launch downloads the Whisper speech model (one-time).
echo       NVIDIA GPU is used automatically when present; otherwise CPU.
echo       Read-aloud playback needs VLC: winget install VideoLAN.VLC
echo       Local neural read-aloud stays on this computer and works offline.
echo.
pause
