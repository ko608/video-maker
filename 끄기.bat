@echo off
chcp 65001 > nul
echo  영상 자판기와 남아 있는 편집 작업을 모두 끄는 중...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*streamlit*' -or $_.Name -like 'ffmpeg*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo  다 껐어요.
timeout /t 2 > nul
