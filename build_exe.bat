@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --noconsole --hidden-import miniaudio --hidden-import _miniaudio --hidden-import _cffi_backend --name NeoGeoADPCMConverter neogeo_adpcm_converter.py
pause
