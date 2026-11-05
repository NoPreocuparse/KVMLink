@echo off
rem Lanzador de kvmlink para Windows: doble clic aqui. Abre la interfaz grafica.
rem Uso por terminal: kvmlink.bat --listen --edge left   (ver README)
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
"%PY%" -c "import pynput, cryptography, pyperclip" 2>nul || (
  echo Instalando dependencias...
  "%PY%" -m pip install -r requirements.txt || (pause & exit /b 1)
)
if not "%~1"=="" (
  rem con argumentos: modo terminal
  "%PY%" kvmlink.py %*
  if errorlevel 1 pause
  exit /b
)
rem sin argumentos: interfaz grafica sin ventana de consola
for %%F in ("%PY%") do set "PYW=%%~dpFpythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" kvmlink.py
