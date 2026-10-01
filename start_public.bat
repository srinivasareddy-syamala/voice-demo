@echo off
REM ==========================================================
REM  Voice AI Demo - run locally + public HTTPS tunnel (Windows)
REM  Double-click this file. Uses a free Cloudflare Quick Tunnel
REM  (no account, no signup) so you can test on your phone.
REM ==========================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PORT=8000
title Voice AI Demo - launcher

REM ---------- 1. Python ----------
set PY=
where python >nul 2>nul && set PY=python
if not defined PY ( where py >nul 2>nul && set PY=py -3 )
if not defined PY (
  echo [X] Python not found. Install Python 3.10+ from https://www.python.org/downloads/ ^(tick "Add to PATH"^)
  pause & exit /b 1
)

REM ---------- 2. Virtual env + packages ----------
if not exist ".venv\Scripts\python.exe" (
  echo [1/5] Creating virtual environment...
  %PY% -m venv .venv || ( echo [X] venv failed & pause & exit /b 1 )
)
echo [2/5] Installing packages ^(first run takes a minute^)...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || ( echo [X] pip install failed & pause & exit /b 1 )
if not exist ".venv\chromium_ok.txt" (
  echo       installing headless Chrome for hard-to-read websites ^(one time^)...
  ".venv\Scripts\python.exe" -m playwright install chromium && echo ok> ".venv\chromium_ok.txt"
)

REM ---------- 3. Settings (.env) ----------
if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo.
  echo [!] Created .env - fill GHL_API_KEY, GHL_LOCATION_ID, GHL_AGENT_ID, save, close Notepad.
  notepad ".env"
)
findstr /r /c:"^GHL_LOCATION_ID=.." ".env" >nul || echo [!] GHL_LOCATION_ID is empty in .env - app runs in DEMO mode ^(agent not updated^).
findstr /r /c:"^GHL_AGENT_ID=.." ".env" >nul || echo [!] GHL_AGENT_ID is empty in .env - app runs in DEMO mode ^(agent not updated^).

REM ---------- 4. Cloudflare tunnel binary ----------
if not exist "cloudflared.exe" (
  echo [3/5] Downloading cloudflared ^(one time^)...
  curl -L --fail -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  if errorlevel 1 ( echo [X] Download failed. Get it manually from https://github.com/cloudflare/cloudflared/releases & pause & exit /b 1 )
)

REM ---------- 5. Start server and WAIT until it answers ----------
echo [4/5] Starting server on http://localhost:%PORT% ...
start "Voice Demo SERVER (close to stop)" cmd /k ".venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port %PORT%"
set /a N=0
:wait_server
set /a N+=1
set CODE=
for /f %%c in ('curl -s -o nul -w "%%{http_code}" http://127.0.0.1:%PORT%/api/config') do set CODE=%%c
if "%CODE%"=="200" goto server_ok
if %N% geq 60 (
  echo [X] Server did not start. Read the error in the "Voice Demo SERVER" window.
  pause & exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait_server
:server_ok
echo       server OK

REM ---------- 6. Tunnel: http2 (works where UDP/QUIC is blocked) ----------
taskkill /im cloudflared.exe /f >nul 2>nul
if exist tunnel.log del /q tunnel.log
echo [5/5] Opening public HTTPS tunnel...
start "Voice Demo TUNNEL (close to stop)" cmd /k "cloudflared.exe tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:%PORT% --logfile tunnel.log"

REM wait for URL + a registered connection to Cloudflare
set URL=
set /a N=0
:wait_tunnel
set /a N+=1
timeout /t 1 /nobreak >nul
if not defined URL for /f "usebackq delims=" %%u in (`powershell -NoProfile -Command "if(Test-Path tunnel.log){(Select-String -Path tunnel.log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1).Matches.Value}"`) do set URL=%%u
findstr /c:"Registered tunnel connection" tunnel.log >nul 2>nul && if defined URL goto tunnel_ok
if %N% geq 60 (
  echo [X] Tunnel did not connect. Check the "Voice Demo TUNNEL" window for errors
  echo     ^(firewall / antivirus / office network blocking cloudflared^).
  pause & exit /b 1
)
goto wait_tunnel
:tunnel_ok
set HOST=%URL:https://=%
echo       tunnel connected: %URL%

REM ---------- 7. Wait until the public URL really works ----------
REM Uses Cloudflare DNS 1.1.1.1 directly, so Windows doesn't cache a "not found" answer.
echo       waiting for the public link to go live ^(10-60 s^)...
set /a N=0
:wait_public
set /a N+=1
timeout /t 2 /nobreak >nul
set IP=
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Resolve-DnsName -Name %HOST% -Server 1.1.1.1 -Type A -DnsOnly -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress} | Select-Object -First 1).IPAddress"`) do set IP=%%i
if not defined IP goto public_retry
set CODE=
for /f %%c in ('curl -s -o nul -w "%%{http_code}" --resolve %HOST%:443:%IP% %URL%/api/config') do set CODE=%%c
if "%CODE%"=="200" goto public_ok
:public_retry
if %N% geq 40 goto public_slow
goto wait_public

:public_slow
echo [!] Link is not answering yet. Wait 1 minute, then open it. If it still fails, see tips below.
goto show

:public_ok
ipconfig /flushdns >nul 2>nul
echo       public link is LIVE

:show
echo.
echo ==========================================================
echo   PUBLIC URL  ^(open on your phone^):
echo   %URL%
echo ==========================================================
echo %URL%| clip
echo   ^(copied to clipboard^)
start "" "%URL%"
echo.
echo Local:  http://localhost:%PORT%
echo.
echo If the browser says "site can't be reached":
echo   1. Wait 30 seconds and refresh - new links take a moment to appear in DNS.
echo   2. On the phone, use mobile data ^(not office Wi-Fi^) or set Private DNS = one.one.one.one
echo   3. On this PC, set DNS to 1.1.1.1 / 1.0.0.1 ^(some ISPs block trycloudflare.com^), then run:
echo        ipconfig /flushdns
echo.
echo To stop: close the SERVER and TUNNEL windows. The link changes every run.
pause
