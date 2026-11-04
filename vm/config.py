"""설정 저장·불러오기 — 키와 모델 이름은 settings.json 한 곳에 보관합니다 (GitHub에는 올라가지 않음)."""
import json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "settings.json"
PROJECTS = ROOT / "projects"
MUSIC = ROOT / "music"
CHANNELS = ROOT / "channels"

def _default_font():
    if sys.platform.startswith("win"):
        for f in [r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf"]:
            if os.path.exists(f):
                return f
    for f in ["/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
              "/System/Library/Fonts/AppleSDGothicNeo.ttc"]:
        if os.path.exists(f):
            return f
    return ""

DEFAULTS = {
    "google_api_key": "",
    "anthropic_api_key": "",
    # 모델 이름 — 바뀌면 설정 화면에서 고칠 수 있습니다
    "claude_model": "claude-sonnet-5",
    "gemini_text_model": "gemini-2.5-flash",          # 유튜브 영상 분석
    "gemini_image_model": "gemini-2.5-flash-image",   # 나노바나나 (이미지)
    "gemini_tts_model": "gemini-2.5-flash-preview-tts",  # 나레이션 목소리
    "veo_model": "veo-3.1-fast-generate-preview",      # 대사 영상 (Veo)
    "tts_voice": "Kore",
    "tts_style": "차분하지만 긴장감 있는 40대 한국 여성 내레이터처럼, 또박또박 자연스럽게 읽어 주세요",
    "font_path": _default_font(),
    "music_volume": 0.35,   # 배경음악 음량 (0~1). 나레이션이 나올 땐 자동으로 더 작아집니다
    "subtitle_size": 76,
    "subtitle_y": 0.55,     # 자막 높이 (0=맨 위, 1=맨 아래). 쇼츠는 화면 중간쯤
    "video_seconds": 4,     # Veo 대사 영상 길이 (4/6/8)
}

def load():
    data = dict(DEFAULTS)
    if SETTINGS.exists():
        try:
            data.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data

def save(data):
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

for d in (PROJECTS, MUSIC):
    d.mkdir(exist_ok=True)
