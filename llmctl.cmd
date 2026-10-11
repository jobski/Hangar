@echo off
rem llmctl: the old name of hangar.cmd, kept so existing scripts and PATH entries still work.
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%~dp0.venv\Scripts\python.exe" -m manager %*
