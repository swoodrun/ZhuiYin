@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "C:\Python314\python.exe" (
  "C:\Python314\python.exe" -B run_portable_tests.py
) else (
  py -3.14 -B run_portable_tests.py
)
pause
