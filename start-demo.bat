@echo off
chcp 65001 >nul
cd /d "%~dp0"
title NVR Monitor

REM --- Otklyuchaem proxy/VPN peremennye dlya etoy sessii (chasta prichina oshibok ustanovki) ---
set "ALL_PROXY="
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "all_proxy="
set "http_proxy="
set "https_proxy="
set "NO_PROXY=127.0.0.1,localhost"

echo ============================================
echo   NVR Monitor - zapusk demo
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python ne nayden.
  echo     Ustanovite ego s https://www.python.org/downloads/
  echo     i OBYAZATELNO postavte galochku "Add Python to PATH".
  echo.
  pause
  exit /b
)

if not exist .venv (
  echo [1/4] Sozdayu okruzhenie...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo [2/4] Ustanavlivayu komponenty ^(pervyy raz 1-2 minuty^)...
python -m pip install --upgrade pip >nul 2>nul
pip install -r requirements.txt
if errorlevel 1 (
  echo [!] Ne udalos ustanovit komponenty. Proverte internet i zapustite snova.
  pause
  exit /b
)

if not exist .env copy .env.example .env >nul

echo [3/4] Zapuskayu emulyator NVR v otdelnom okne...
start "Mock NVR (ne zakryvat)" cmd /k ".venv\Scripts\activate.bat && set ""MOCK_PORT=8088"" && python -m mock.server"

timeout /t 4 >nul
start "" http://localhost:8000

echo [4/4] Zapuskayu prilozhenie.
echo.
echo ============================================
echo   Otkroyte v brauzere:  http://localhost:8000
echo   Dlya ostanovki: zakroyte oba chernyh okna.
echo   ETO OKNO NE ZAKRYVAYTE.
echo ============================================
echo.
uvicorn app.main:app
pause
