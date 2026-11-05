@echo off
rem Instalador de kvmlink para Windows: doble clic aqui.
rem Crea los accesos directos, el inicio al iniciar sesion y la entrada para desinstalarlo.
setlocal
cd /d "%~dp0"
set "PY="
for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i WindowsApps') do if not defined PY set "PY=%%P"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY (
  echo No se ha encontrado Python. Instala Python 3 desde https://www.python.org/downloads/
  pause
  exit /b 1
)
"%PY%" install.py %*
echo.
pause
