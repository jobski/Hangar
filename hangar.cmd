@echo off
rem hangar: the manager from the command line. Put this folder on PATH to use it anywhere.
setlocal
rem python -m looks for the package in the current folder, so point it at the install folder: works from anywhere
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%~dp0.venv\Scripts\python.exe" -m manager %*
