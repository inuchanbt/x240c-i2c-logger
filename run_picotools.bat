@echo off
cd /d "%~dp0"
py x240c_i2c_logger.py --picotools %*
pause
