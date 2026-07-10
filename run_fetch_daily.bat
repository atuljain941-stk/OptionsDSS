@echo off
set ROOT=%~dp0
echo --- RUN %date% %time% --- 
cd /d "%ROOT%"
call .venv\Scripts\activate
python run_fetch_once.py
echo --- DONE ---
