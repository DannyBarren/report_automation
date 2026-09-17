@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title GenerSwift
echo.
echo  ========================================
echo   GenerSwift - Windows one-command start
echo  ========================================
echo.

REM --- Python ---
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python was not found on PATH.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         Check "Add python.exe to PATH" during setup, then re-run this file.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2 delims==" %%I in ('python -c "import sys; print(sys.version_info[0:2])" 2^>nul') do set PYVER=%%I
echo [OK] Found Python %PYVER%

REM --- Virtual environment ---
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [1/4] Creating virtual environment .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Could not create .venv
    pause
    exit /b 1
  )
) else (
  echo [OK] Virtual environment .venv exists
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
  echo [ERROR] Could not activate .venv
  pause
  exit /b 1
)

REM --- pip dependencies ---
echo.
echo [2/4] Installing Python packages (requirements.txt) ...
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed. Check your internet connection and retry.
  pause
  exit /b 1
)
echo [OK] Python dependencies installed

REM --- GTK3 / WeasyPrint (PDF) ---
echo.
echo [3/4] Checking PDF libraries (GTK3 + WeasyPrint) ...
set JOBDOC_PREFER_PORTABLE_GTK=1

python -m utils.setup_weasyprint --check-only
set CHECK_EXIT=%ERRORLEVEL%

if %CHECK_EXIT%==0 (
  echo [OK] WeasyPrint is ready for PDF export
  goto :apply_gtk_env
)

echo.
echo  --------------------------------------------------------
echo   GTK3 runtime is required for PDF reports (one-time setup)
echo  --------------------------------------------------------
echo   WeasyPrint needs Pango/Cairo DLLs. This demo downloads
echo   the official GTK package (~49 MB) into gtk3\runtime
echo   inside this project folder — no system-wide install.
echo.
echo   Choose one path:
echo.
echo   (A) Run as Administrator — RECOMMENDED if portable fails
echo       Right-click run_demo.bat -^> Run as administrator
echo       Then run this file again. Silent install works once.
echo.
echo   (B) Portable extract — NO admin (default, trying now)
echo       Extracts DLLs with 7-Zip or py7zr into gtk3\runtime.
echo       Install 7-Zip from https://www.7-zip.org/ if extract fails.
echo.
echo   After the app starts: http://127.0.0.1:5000/setup/weasyprint
echo  --------------------------------------------------------
echo.

set JOBDOC_AUTO_INSTALL_GTK=1
python -m utils.setup_weasyprint --ensure-gtk
set SETUP_EXIT=%ERRORLEVEL%

if %SETUP_EXIT%==0 (
  echo [OK] WeasyPrint is ready for PDF export
  goto :apply_gtk_env
)

REM If portable failed and we are not elevated, try installer only when admin
python -c "import sys; from utils.setup_weasyprint import is_process_elevated; sys.exit(0 if is_process_elevated() else 1)"
if not errorlevel 1 (
  echo.
  echo [INFO] Running as Administrator — trying silent installer ...
  set JOBDOC_ALLOW_GTK_INSTALLER=1
  python -m utils.setup_weasyprint --ensure-gtk --allow-installer
  if not errorlevel 1 (
    echo [OK] WeasyPrint is ready after installer
    goto :apply_gtk_env
  )
)

echo.
echo [WARN] PDF libraries are not ready yet.
echo        Recording and analysis will still work.
echo        PDF export will show a Fix Dependencies page until GTK is set up.
echo.
echo        Next steps:
echo          1. Right-click run_demo.bat -^> Run as administrator, run again
echo          2. Or install 7-Zip and run again (portable extract)
echo          3. Open http://127.0.0.1:5000/setup/weasyprint in your browser
echo.

:apply_gtk_env
for /f "usebackq delims=" %%L in (`python -m utils.setup_weasyprint --emit-env 2^>nul`) do %%L

REM --- Launch Flask ---
echo.
echo [4/4] Starting GenerSwift on http://127.0.0.1:5000
echo       PDF setup help: http://127.0.0.1:5000/setup/weasyprint
echo       Health check:   http://127.0.0.1:5000/health
echo       On your phone (same Wi-Fi): http://YOUR-PC-IP:5000
echo       Press Ctrl+C to stop.
echo.

set APP_EXIT=0
python app.py
set APP_EXIT=%ERRORLEVEL%

if not %APP_EXIT%==0 (
  echo.
  echo [ERROR] app.py exited with code %APP_EXIT%
  pause
)

endlocal
exit /b %APP_EXIT%
