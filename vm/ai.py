"""AI 호출 모음 — 영상 분석(제미나이), 대본(클로드), 이미지·영상·목소리(구글)."""
import base64, json, re, time, wave
from pathlib import Path


# ---------------- 공통 ----------------
def _gclient(cfg):
    from google import genai
    if not cfg.get("google_api_key"):
        raise RuntimeError("설정에서 구글 AI Studio 키를 넣어 주세요.")
    return genai.Client(api_key=cfg["google_api_key"])


def _claude(cfg, prompt, max_tokens=8000):
    import anthropic
    if not cfg.get("anthropic_api_key"):
        raise RuntimeError("설정에서 Anthropic 키를 넣어 주세요.")
    c = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    r = c.messages.create(model=cfg["claude_model"], max_tokens=max_tokens,
                          messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


def _json_from(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("AI 답에서 대본(JSON)을 찾지 못했어요. 다시 눌러 주세요.")
    return json.loads(m.group(0))


FALLBACK_TEXT = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-pro-latest", "gemini-3.1-pro-preview"]


def _with_retry(fn, models):
    """구글이 '사용자 많음(503)'일 때 잠깐 쉬었다 다시, 그래도 안 되면 다른 모델로."""
    last = None
    seen = []
    for m in models:
        if not m or m in seen:
            continue
        seen.append(m)
        for wait in (0, 5, 15):
            if wait:
                time.sleep(wait)
            try:
                return fn(m)
            except Exception as e:
                last = e
                msg = str(e)
                if any(k in msg for k in ("404", "NOT_FOUND", "no longer available", "not supported")):
                    break  # 이 모델은 못 씀 → 바로 다음 모델로
                if not any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded", "500")):
                    raise
    raise RuntimeError("구글 모델을 쓸 수 없어요(바쁘거나 막힘). 몇 분 뒤 다시 눌러 주세요. 계속되면 이 화면을 Claude에게 보내 주세요. (" + str(last)[:150] + ")")


# ---------------- 1. 잘 되는 영상 분석 ----------------
ANALYZE_PROMPT = """당신은 한국 유튜브 쇼츠·롱폼 성공 분석가입니다. 이 영상을 처음부터 끝까지 보고 듣고, 아래 형식의 한국어 보고서를 써 주세요.
60대도 읽기 쉬운 말로, 시간은 "0:07"처럼 표시하세요.

## 한 줄 요약
## 첫 3초 후킹 (무슨 장면·말로 붙잡았나)
## 장면 흐름 시간표 (시간 — 장면 — 대사/나레이션 요지 — 감정)
## 반전 지점 (몇 초에, 무엇이 뒤집혔나)
## 잘된 점 (장점) 5가지
## 아쉬운 점 (단점) 3가지 — 지루하거나 이탈할 만한 구간
## 제목·썸네일 공식 (쓰인 단어, 구도, 숫자)
## 우리 채널에 가져올 공식 5가지 — "구조"만, 줄거리·대사 베끼기 금지
"""


def analyze_youtube(cfg, url, extra=""):
    from google.genai import types
    client = _gclient(cfg)
    contents = types.Content(parts=[
        types.Part(file_data=types.FileData(file_uri=url.strip())),
        types.Part(text=ANALYZE_PROMPT + (f"\n참고 메모: {extra}" if extra else "")),
    ])
    r = _with_retry(lambda m: client.models.generate_content(model=m, contents=contents),
                    [cfg["gemini_text_model"]] + FALLBACK_TEXT)
    return (r.text or "").strip()


# ---------------- 2. 우리식 대본 ----------------
SCRIPT_PROMPT = """당신은 조회수가 잘 나오는 한국 유튜브 스토리 쇼츠 작가입니다.
아래 [채널 규칙]을 반드시 지켜 새 대본을 쓰세요.

[채널] {name}
[채널 규칙]
{rules}

[참고: 잘 된 영상 분석 — 구조와 리듬만 참고하고, 인물·사연·대사는 완전히 새로 쓸 것. 비슷한 문장이 나오면 안 됨]
{analysis}

[대표님 요청 주제/메모]
{topic}

아래 JSON 하나만 출력하세요. 다른 말은 쓰지 마세요.
{{
 "title_options": ["제목 후보 3개 — 첫 대사 인용 + 비밀/반전 암시"],
 "description": "유튜브 설명 3~4줄 (줄바꿈은 \\n)",
 "tags": "쉼표로 구분한 태그 한 줄 (15개 안팎)",
 "pinned_comment": "고정 댓글 한 줄",
 "thumbnail": ["썸네일 1줄(흰 글씨, 짧게)", "2줄(노란 큰 글씨, 핵심)", "3줄(작게, 궁금증)"],
 "music_mood": "배경음악 분위기 한 줄",
 "characters": "등장인물 외모 설명(영어, 모든 장면에서 같은 사람으로 보이게): 나이·머리·옷",
 "cuts": [
  {{"no": 1, "kind": "video", "line": "대사(따옴표 없이, 15자 안팎)", "speaker": "누가 누구에게", "narration": "", "visual": "영어 장면 묘사 (인물·행동·장소·표정)"}},
  {{"no": 2, "kind": "image", "line": "", "speaker": "", "narration": "나레이션 90~130자", "visual": "영어 장면 묘사"}}
 ]
}}
cuts는 정확히 {ncuts}개, 규칙의 컷 구성을 따르세요."""


def write_script(cfg, channel, analysis="", topic=""):
    prompt = SCRIPT_PROMPT.format(
        name=channel["name"], rules="\n".join("- " + r for r in channel["rules"]),
        analysis=analysis or "(분석 없음)", topic=topic or "(자유 주제)", ncuts=channel["cuts"])
    data = _json_from(_claude(cfg, prompt))
    up = channel.get("upload", {})
    if up.get("description_tail") and up["description_tail"] not in data.get("description", ""):
        data["description"] = data.get("description", "").rstrip() + "\n" + up["description_tail"]
    if up.get("base_tags"):
        have = [t.strip() for t in data.get("tags", "").split(",") if t.strip()]
        for t in up["base_tags"].split(","):
            if t not in have:
                have.append(t)
        data["tags"] = ",".join(have)
    return data


# ---------------- 3. 이미지 (나노바나나) ----------------
def make_image(cfg, prompt, out_path, aspect="9:16"):
    from google.genai import types
    client = _gclient(cfg)
    r = _with_retry(lambda m: client.models.generate_content(
        model=m, contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"],
                                           image_config=types.ImageConfig(aspect_ratio=aspect))),
        [cfg["gemini_image_model"], "gemini-nano-banana-2.1", "gemini-3.1-flash-image", "gemini-3-pro-image", "gemini-3.1-flash-image-preview"])
    for cand in r.candidates or []:
        for part in cand.content.parts or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                data = part.inline_data.data
                if isinstance(data, str):
                    data = base64.b64decode(data)
                Path(out_path).write_bytes(data)
                return out_path
    raise RuntimeError("이미지가 만들어지지 않았어요 (안전 필터에 걸렸을 수 있어요). 장면 묘사를 바꿔 다시 해 보세요.")


# ---------------- 4. 대사 영상 (Veo) ----------------
def make_video(cfg, prompt, out_path, first_frame=None, aspect="9:16", log=print):
    from google.genai import types
    client = _gclient(cfg)
    kw = dict(model=cfg["veo_model"], prompt=prompt,
              config=types.GenerateVideosConfig(aspect_ratio=aspect,
                                                duration_seconds=int(cfg.get("video_seconds", 4)),
                                                number_of_videos=1))
    if first_frame and Path(first_frame).exists():
        mime = "image/png" if str(first_frame).lower().endswith(".png") else "image/jpeg"
        kw["image"] = types.Image(image_bytes=Path(first_frame).read_bytes(), mime_type=mime)
    op = client.models.generate_videos(**kw)
    waited = 0
    while not op.done:
        time.sleep(8); waited += 8
        log(f"   … 영상 만드는 중 ({waited}초)")
        op = client.operations.get(op)
        if waited > 600:
            raise RuntimeError("영상이 10분 넘게 안 나와서 멈췄어요.")
    vids = getattr(op.response, "generated_videos", None) or []
    if not vids:
        raise RuntimeError("영상이 만들어지지 않았어요 (안전 필터일 수 있어요).")
    v = vids[0]
    client.files.download(file=v.video)
    v.video.save(str(out_path))
    return out_path


# ---------------- 5. 나레이션 목소리 ----------------
def make_tts(cfg, text, out_wav):
    from google.genai import types
    client = _gclient(cfg)
    r = _with_retry(lambda m: client.models.generate_content(
        model=m,
        contents=f"{cfg.get('tts_style','')}:\n{text}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.get("tts_voice", "Kore")))))),
        [cfg["gemini_tts_model"], "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-3.8-flash-lite-tts"])
    data = r.candidates[0].content.parts[0].inline_data.data
    if isinstance(data, str):
        data = base64.b64decode(data)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(data)
    return out_wav


# ---------------- 연결 시험 ----------------
def list_google_models(cfg):
    client = _gclient(cfg)
    return sorted(m.name.replace("models/", "") for m in client.models.list())
