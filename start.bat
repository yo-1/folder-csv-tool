@echo off
setlocal
pushd "%~dp0"
py -3 -c "import tkinter" >nul 2>&1
if not errorlevel 1 (
    py -3 folder_csv.py
    if errorlevel 1 pause
    popd
    exit /b
)
python -c "import sys, tkinter; assert sys.version_info.major == 3" >nul 2>&1
if not errorlevel 1 (
    python folder_csv.py
    if errorlevel 1 pause
    popd
    exit /b
)
echo Python 3 with tkinter is required.
echo Install standard Windows Python, including Tcl/Tk and the launcher.
echo If Python is already installed, run folder_csv.py using that Python.
pause
popd
