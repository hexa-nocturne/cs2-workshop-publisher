@echo off
rem Windows launcher: workshop <command> [options]
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0workshop.py" %*
) else (
  python "%~dp0workshop.py" %*
)
exit /b %errorlevel%
