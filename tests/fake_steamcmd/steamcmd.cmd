@echo off
"%FAKE_STEAMCMD_PYTHON%" "%~dp0fake_steamcmd.py" %*
exit /b %errorlevel%
