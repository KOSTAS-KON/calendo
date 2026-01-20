@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ===== Therapy+SMS Product Launcher (Client Friendly) =====
REM - Starts Docker Desktop if needed
REM - Runs: docker compose up -d --build
REM - Opens: http://localhost:8080/
REM - Creates a Desktop shortcut (TherapySMS.lnk)

cd /d "%~dp0"

REM Always point docker compose to the local compose file (avoids "no configuration file" errors)
set "COMPOSE_FILE=%~dp0docker-compose.yml"
if not exist "%COMPOSE_FILE%" (
  echo.
  echo ERROR: docker-compose.yml not found next to Start.bat
  echo Expected: %COMPOSE_FILE%
  echo.
  echo Please extract the ZIP fully and run Start.bat from inside the extracted folder.
  echo.
  pause
  exit /b 1
)

REM Create a proper Desktop .lnk shortcut (opens landing page)
set "DESKTOP=%USERPROFILE%\Desktop"
set "SHORTCUT=%DESKTOP%\TherapySMS.lnk"
if not exist "%SHORTCUT%" (
  if exist "%~dp0CreateShortcut.vbs" (
    wscript //nologo "%~dp0CreateShortcut.vbs" >nul 2>&1
  )
)

REM Check Docker daemon
docker info >nul 2>&1
if %errorlevel% neq 0 (
  echo Docker Desktop does not appear to be running. Starting it now...
  start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" >nul 2>&1
  echo Waiting for Docker to start...
  set /a _tries=0
  :wait_docker
  docker info >nul 2>&1
  if %errorlevel% equ 0 goto docker_ok
  set /a _tries+=1
  if !_tries! gtr 60 (
    echo.
    echo ERROR: Docker did not start within expected time.
    echo Please start Docker Desktop manually, then run Start.bat again.
    echo.
    pause
    exit /b 1
  )
  timeout /t 2 /nobreak >nul
  goto wait_docker
)
:docker_ok

echo Starting Therapy Portal + SMS Calendar...

docker compose -f "%COMPOSE_FILE%" up -d --build
if %errorlevel% neq 0 (
  echo.
  echo ERROR: Failed to start containers.
  echo Run 'docker compose -f "%COMPOSE_FILE%" logs --tail=200' for details.
  echo.
  pause
  exit /b 1
)

echo.
echo Opening the app in your browser...
start "" "http://localhost:8080/"

echo.
echo Done. You can close this window.
echo (To stop the app, run Stop.bat)

endlocal
