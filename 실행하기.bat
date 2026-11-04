@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo  새 버전 확인 중...
git pull -q
py -m pip install -q -U -r requirements.txt
echo  영상 자판기를 켜는 중... 잠시 뒤 인터넷 창이 열립니다. (이 검은 창은 닫지 마세요)
py -m streamlit run app.py --server.port 8501
pause
