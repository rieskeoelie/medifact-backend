@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM Medifact — LOCAL BACKEND LAUNCHER (Windows)
REM Dubbelklik dit bestand om de backend lokaal te starten.
REM ─────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0"

echo.
echo  ╔════════════════════════════════════════════════════╗
echo  ║    Medifact API — Opstarten        ║
echo  ╚════════════════════════════════════════════════════╝
echo.

REM ── 1. Check Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python niet gevonden.
    echo  Installeer Python via: https://www.python.org/downloads/
    echo  Vink bij installatie "Add Python to PATH" aan!
    pause
    exit /b 1
)
echo  OK  Python gevonden

REM ── 2. Check ANTHROPIC_API_KEY ───────────────────────────────────────────────
if exist .env (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if "%%a"=="ANTHROPIC_API_KEY" set ANTHROPIC_API_KEY=%%b
    )
)

if "%ANTHROPIC_API_KEY%"=="" (
    echo.
    echo  WAARSCHUWING: ANTHROPIC_API_KEY niet gevonden.
    set /p ANTHROPIC_API_KEY="  Voer je API-sleutel in: "
    if "%ANTHROPIC_API_KEY%"=="" (
        echo  ERROR: Geen API-sleutel. Stop.
        pause
        exit /b 1
    )
    echo ANTHROPIC_API_KEY=%ANTHROPIC_API_KEY%> .env
    echo  OK  API-sleutel opgeslagen in .env
) else (
    echo  OK  ANTHROPIC_API_KEY gevonden
)

REM ── 3. Installeer packages ───────────────────────────────────────────────────
echo.
echo  Controleer Python-pakketten...
pip install -q -r requirements.txt
echo  OK  Pakketten klaar

REM ── 4. Start server ──────────────────────────────────────────────────────────
echo.
echo  Medifact API start op http://localhost:8000
echo  Open medifact.html in je browser nadat de server klaar is.
echo  Stop met: Ctrl+C
echo.
echo  ────────────────────────────────────────────────────────
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
pause
