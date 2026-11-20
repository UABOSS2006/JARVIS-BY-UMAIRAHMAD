@echo off
REM Rebuilds JARVIS into dist\JARVIS\JARVIS.exe  (run from this folder)
cd /d "%~dp0"

echo [1/4] Stopping old instances...
taskkill /f /im JARVIS.exe >nul 2>&1

echo [2/4] Cleaning old build output...
rmdir /s /q build dist >nul 2>&1
del /q *.spec >nul 2>&1

echo [3/4] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

echo [4/4] Building...
python -m PyInstaller --noconfirm --onedir --noconsole --name JARVIS ^
  --add-data "index.html;." ^
  --hidden-import pyttsx3.drivers.sapi5 --hidden-import comtypes.client ^
  --hidden-import speech_recognition ^
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all webview ^
  app.py

echo.
echo Done. Run: dist\JARVIS\JARVIS.exe  (keep the _internal folder next to it)
pause
