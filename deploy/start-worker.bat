@echo off
rem Start the separation worker. Edit worker.env (next to this file) first.
cd /d "%~dp0"
if exist worker.env (
  for /f "usebackq tokens=1,* delims==" %%a in ("worker.env") do (
    if not "%%a"=="" if not "%%a:~0,1%"=="#" set "%%a=%%b"
  )
)
if "%INSTR_SERVER%"=="" (
  echo Set INSTR_SERVER in worker.env, e.g. INSTR_SERVER=http://192.168.1.10:8899
  pause
  exit /b 1
)
".venv\Scripts\python.exe" worker.py
pause
