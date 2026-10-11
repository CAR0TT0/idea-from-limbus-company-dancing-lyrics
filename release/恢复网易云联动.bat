@echo off
setlocal
cd /d "%~dp0"
if not exist "FloatingLyrics.exe" (
  echo Please extract the entire ZIP before running this file.
  pause
  exit /b 1
)
start "" "FloatingLyrics.exe" --restore-netease
if errorlevel 1 (
  echo The recovery window could not start. Keep the entire _internal folder.
  pause
  exit /b 1
)
