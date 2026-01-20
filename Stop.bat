@echo off
setlocal

cd /d "%~dp0"

set "COMPOSE_FILE=%~dp0docker-compose.yml"
if not exist "%COMPOSE_FILE%" (
  echo.
  echo ERROR: docker-compose.yml not found next to Stop.bat
  echo Expected: %COMPOSE_FILE%
  echo.
  pause
  exit /b 1
)

echo Stopping Therapy Portal + SMS Calendar...

docker compose -f "%COMPOSE_FILE%" down
if %errorlevel% neq 0 (
  echo.
  echo ERROR: Failed to stop containers.
  echo.
  pause
  exit /b 1
)

echo.
echo Done.
endlocal
