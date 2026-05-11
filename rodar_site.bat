@echo off
cd /d "%~dp0"

set "PYTHON_EXE="

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  goto :run
)

for /f "delims=" %%P in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\Python*\python.exe" 2^>nul') do (
  set "PYTHON_EXE=%%P"
  goto :run
)

where py >nul 2>nul
if %errorlevel%==0 (
  set "PYTHON_EXE=py"
  goto :run
)

where python >nul 2>nul
if %errorlevel%==0 (
  set "PYTHON_EXE=python"
  goto :run
)

echo Python nao encontrado.
echo Instale o Python em https://www.python.org/downloads/ e marque a opcao "Add python.exe to PATH".
pause
goto :eof

:run
"%PYTHON_EXE%" -m pip install -r requirements.txt
"%PYTHON_EXE%" -m streamlit run app.py
goto :eof
