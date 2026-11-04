@echo off
chcp 65001 > nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8501 ^| findstr LISTENING') do taskkill /PID %%a /F > nul
echo  영상 자판기를 껐어요.
timeout /t 2 > nul
