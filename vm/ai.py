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
    if max_tokens > 8000:   # 긴 대본(롱폼)은 끊기지 않게 스트리밍으로 받기
        with c.messages.stream(model=cfg["claude_model"], max_tokens=max_tokens,
                               messages=[{"role": "user", "content": prompt}]) as st_:
            return st_.get_final_text().strip()
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
                if "exceeded your current quota" in msg or "billing" in msg:
                    if "limit: 0" in msg or "free_tier" in msg.lower():
                        raise RuntimeError("구글 무료 한도로는 이 기능(그림·목소리)을 쓸 수 없어요. AI Studio에서 결제(Billing)를 켜거나, 플로우에서 만들어 올려 주세요.")
                    # 결제는 켜져 있는데 '1분당 횟수' 한도에 걸림 → 1분쯤 쉬었다 같은 모델로 다시
                    for w2 in (30, 45, 60):
                        time.sleep(w2)
                        try:
                            return fn(m)
                        except Exception as e2:
                            last = e2
                            if "exceeded your current quota" not in str(e2):
                                break
                    raise RuntimeError("구글 사용 한도에 걸렸어요. ① 1분당 횟수 한도면 몇 분 뒤 다시 누르면 이어서 만들어요 "
                                       "(이미 만든 건 그대로 둠). ② AI Studio에서 정한 월 사용 한도(지출 한도)에 닿았으면 한도를 올려 주세요. ("
                                       + str(last)[:200] + ")")
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
  {{"no": 1, "kind": "video", "line": "대사(따옴표 없이, 15자 안팎)", "speaker": "누가 누구에게", "speaker_en": "English: ONLY the person who speaks + look, e.g. the daughter-in-law (woman in her 30s, ponytail, beige cardigan)", "narration": "", "visual": "영어 장면 묘사 (인물·행동·장소·표정)"}},
  {{"no": 2, "kind": "image", "line": "", "speaker": "", "narration": "나레이션 90~130자", "visual": "영어 장면 묘사"}}
 ]
}}
cuts는 정확히 {ncuts}개, 규칙의 컷 구성을 따르세요.
각 컷의 visual에는 시대·장소를 구체적으로 쓰세요(예: 1970s shabby thatched-roof mountain tavern, small dim-free room with paper doors). 각 컷의 visual은 서로 확실히 달라야 합니다 — 장소·행동·카메라 구도(클로즈업/전신/손 클로즈업 등)·등장인물 조합을 컷마다 바꾸고, 같은 문장을 되풀이하지 마세요."""


LONG_SCRIPT_PROMPT = """당신은 조회수가 잘 나오는 한국 유튜브 '오디오 드라마형' 롱폼(10~15분) 작가입니다.
화면은 장면 그림 + 나레이션(인물 대사 포함)으로 진행됩니다. 아래 [채널 규칙]을 반드시 지키세요.

[채널] {name}
[채널 규칙]
{rules}

[참고: 잘 된 영상 분석 — 구조와 리듬만 참고, 인물·사연·대사는 완전히 새로]
{analysis}

[대표님 요청 주제/메모]
{topic}

아래 JSON 하나만 출력하세요. 다른 말은 쓰지 마세요.
{{
 "title_options": ["제목 후보 3개 — '○○인 줄 알았는데, △△였습니다｜짧은 대사' 형식"],
 "description": "유튜브 설명 4~6줄 (줄바꿈은 \\n). 마지막 줄은 '※ 이 영상은 창작 드라마이며 등장인물과 사건은 모두 가상입니다.'",
 "tags": "쉼표로 구분한 태그 한 줄 (15개 안팎)",
 "pinned_comment": "고정 댓글 한 줄 (시청자 경험을 묻는 질문)",
 "thumbnail": ["윗줄 8자 이내(상황)", "핵심 한마디 6~8자(가장 궁금한 대사·반전)", ""],
 "music_mood": "배경음악 분위기 한 줄",
 "characters": "등장인물 외모 설명(영어, 모든 장면에서 같은 사람으로 보이게): 이름·나이·머리·옷",
 "chapters": [
  {{"title": "장 제목(짧게)", "cuts": [
    {{"narration": "나레이션 150~230자. 인물 대사는 \"큰따옴표\"로 넣어 생생하게", "visual": "영어 장면 묘사: 시대·장소 구체적으로, 인물·행동·표정, 카메라 구도"}}
  ]}}
 ]
}}
chapters는 {nchap}개, 전체 cuts 합계는 정확히 {ncuts}개 안팎(장마다 비슷하게 나눔). 나레이션은 컷마다 150~230자를 꼭 지켜 전체 길이를 맞추세요.
각 장의 마지막 컷 나레이션은 다음 장이 궁금해지는 한 문장('그런데 그날 밤, …')으로 끝내세요.
컷마다 visual은 서로 확실히 달라야 합니다(장소·구도·인물 조합을 바꿀 것)."""


def _write_long(cfg, channel, analysis, topic):
    prompt = LONG_SCRIPT_PROMPT.format(
        name=channel["name"], rules="\n".join("- " + r for r in channel["rules"]),
        analysis=analysis or "(분석 없음)", topic=topic or "(자유 주제)",
        ncuts=channel["cuts"], nchap=channel.get("chapters", 5))
    data = _json_from(_claude(cfg, prompt, max_tokens=min(64000, int(channel['cuts']) * 450 + 3000)))
    cuts, n = [], 0
    for ch in data.get("chapters", []):
        for i, c in enumerate(ch.get("cuts", [])):
            n += 1
            cuts.append({"no": n, "kind": "image", "chapter": ch.get("title", "") if i == 0 else "",
                         "line": "", "speaker": "", "narration": c.get("narration", ""), "visual": c.get("visual", "")})
    data["cuts"] = cuts
    data.pop("chapters", None)
    return data


def write_script(cfg, channel, analysis="", topic=""):
    if channel.get("format") == "longform":
        data = _write_long(cfg, channel, analysis, topic)
    else:
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
    data["format"] = channel.get("format", "shorts")
    return data


# ---------------- 2-1. 한 단어 → 주제 후보 ----------------
def suggest_topics(cfg, channel, keyword):
    prompt = f"""당신은 조회수가 잘 나오는 한국 유튜브 스토리 작가입니다.
[채널] {channel['name']}
[채널 규칙]
{chr(10).join('- ' + r for r in channel['rules'])}

키워드: "{keyword or '자유'}"
이 키워드로 시청자가 끝까지 볼 이야기 주제 4개를 만드세요. 서로 완전히 다른 이야기로(감동·갈등·진상 등 섞기).
아래 JSON 하나만 출력하세요.
{{"ideas": [{{"title": "주제 제목(20자 안팎, 궁금증 생기게)", "twist": "핵심 반전 한 줄(30자 안팎)"}}]}}"""
    data = _json_from(_claude(cfg, prompt, max_tokens=1500))
    return data.get("ideas", [])[:4]


# ---------------- 3. 이미지 (나노바나나) ----------------
def make_image(cfg, prompt, out_path, aspect="9:16", ref=None):
    """ref: 등장인물 기준 그림 — 넣으면 모든 컷에서 같은 사람으로 그림"""
    from google.genai import types
    client = _gclient(cfg)
    contents = prompt
    if ref and Path(ref).exists():
        mime = "image/png" if str(ref).lower().endswith(".png") else "image/jpeg"
        contents = [types.Part.from_bytes(data=Path(ref).read_bytes(), mime_type=mime),
                    "The attached image is the CHARACTER REFERENCE. Keep exactly the same people "
                    "(same faces, hair, age, clothes) in this new scene. Do not copy its layout. " + prompt]
    def call(m):
        # 고화질(2K)로 먼저 요청 → 모델이 못 하면 기본 크기로
        for size in (cfg.get("image_size", "2K"), None):
            ic = types.ImageConfig(aspect_ratio=aspect, image_size=size) if size else types.ImageConfig(aspect_ratio=aspect)
            try:
                return client.models.generate_content(
                    model=m, contents=contents,
                    config=types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=ic))
            except Exception as e:
                if size and any(k in str(e) for k in ("400", "INVALID_ARGUMENT", "image_size")):
                    continue
                raise

    r = _with_retry(call,
        [cfg["gemini_image_model"], "gemini-nano-banana-2.1", "gemini-3.1-flash-image", "gemini-3.1-flash-image-preview"])
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
    img = None
    if first_frame and Path(first_frame).exists():
        mime = "image/png" if str(first_frame).lower().endswith(".png") else "image/jpeg"
        img = types.Image(image_bytes=Path(first_frame).read_bytes(), mime_type=mime)
    # 1순위: 구글 새 영상 모델 Gemini Omni Flash (Interactions API) — 플로우와 같은 모델
    errors = []
    try:
        return _omni_video(client, cfg, prompt, first_frame, out_path, aspect, log)
    except Exception as e:
        msg = str(e)
        if "exceeded your current quota" in msg or "billing" in msg.lower():
            raise RuntimeError("대사 영상은 구글 결제(Billing)를 켜야 만들 수 있어요.")
        errors.append(f"omni: {msg[:120]}")
    # 예비: 예전 방식(Veo 등)
    models = []
    for m in [cfg.get("veo_model"), "gemini-omni-1.1-flash", "gemini-omni-flash-preview",
              "veo-3.1-fast-generate-preview", "veo-3.1-lite-generate-preview", "veo-3.1-generate-preview"]:
        if m and m not in models:
            models.append(m)
    for m in models:
        kw = dict(model=m, prompt=prompt,
                  config=types.GenerateVideosConfig(aspect_ratio=aspect,
                                                    duration_seconds=int(cfg.get("video_seconds", 4)),
                                                    number_of_videos=1))
        if img is not None:
            kw["image"] = img
        try:
            op = client.models.generate_videos(**kw)
        except Exception as e:
            msg = str(e)
            if "exceeded your current quota" in msg or "billing" in msg.lower():
                raise RuntimeError("대사 영상은 구글 결제(Billing)를 켜야 만들 수 있어요.")
            errors.append(f"{m}: {msg[:80]}")
            continue
        log(f"   ({m} 모델로 만드는 중)")
        waited = 0
        while not op.done:
            time.sleep(8); waited += 8
            log(f"   … 영상 만드는 중 ({waited}초)")
            op = client.operations.get(op)
            if waited > 600:
                raise RuntimeError("영상이 10분 넘게 안 나와서 멈췄어요.")
        vids = getattr(op.response, "generated_videos", None) or []
        if not vids:
            raise RuntimeError("영상이 만들어지지 않았어요 (안전 필터일 수 있어요). 장면 묘사를 바꿔 보세요.")
        v = vids[0]
        client.files.download(file=v.video)
        v.video.save(str(out_path))
        return out_path
    raise RuntimeError("이 구글 키로 쓸 수 있는 영상 모델이 없어요 → 플로우로 만들어 올려 주세요. (" + " / ".join(errors)[:300] + ")")


def _omni_video(client, cfg, prompt, first_frame, out_path, aspect, log):
    model = cfg.get("omni_model") or "gemini-omni-1.1-flash"
    if not hasattr(client, "interactions"):
        raise RuntimeError("구글 프로그램(google-genai)이 옛 버전이에요. 실행하기.bat으로 다시 켜면 새로 받아요.")
    text = prompt + f" About {int(cfg.get('video_seconds', 4))} seconds long, single continuous shot."
    if first_frame and Path(first_frame).exists():
        mime = "image/png" if str(first_frame).lower().endswith(".png") else "image/jpeg"
        b64 = base64.b64encode(Path(first_frame).read_bytes()).decode()
        inp = [{"type": "image", "data": b64, "mime_type": mime},
               {"type": "text", "text": "Use this image as the first frame. " + text}]
    else:
        inp = text
    log(f"   ({model} 모델로 영상 만드는 중 — 1~3분)")
    it = client.interactions.create(model=model, input=inp,
                                    response_format={"type": "video", "aspect_ratio": aspect, "delivery": "uri"})
    vo = it.output_video
    if getattr(vo, "data", None):
        data = vo.data
        Path(out_path).write_bytes(base64.b64decode(data) if isinstance(data, str) else data)
        return out_path
    uri = getattr(vo, "uri", None)
    if not uri:
        raise RuntimeError("영상이 만들어지지 않았어요 (안전 필터일 수 있어요).")
    name = uri.split("/")[-1].split("?")[0]
    waited = 0
    while True:
        st_ = client.files.get(name=f"files/{name}").state.name
        if st_ == "ACTIVE":
            break
        if st_ == "FAILED":
            raise RuntimeError("영상 만들기 실패 (안전 필터일 수 있어요).")
        time.sleep(5); waited += 5
        log(f"   … 영상 마무리 중 ({waited}초)")
        if waited > 600:
            raise RuntimeError("영상이 10분 넘게 안 나와서 멈췄어요.")
    try:
        client.files.download(file=uri, destination=str(out_path))
    except TypeError:
        data = client.files.download(file=uri)
        Path(out_path).write_bytes(data)
    if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
        data = client.files.download(file=uri)
        Path(out_path).write_bytes(data)
    return out_path


# ---------------- 5. 나레이션 목소리 ----------------
def make_tts(cfg, text, out_wav):
    from google.genai import types
    client = _gclient(cfg)
    voice = types.SpeechConfig(voice_config=types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.get("tts_voice", "Kore"))))
    style = (cfg.get("tts_style") or "").strip()

    def call(m):
        # 말투 지시는 '읽을 글'과 분리해서 보냄 → 지시문을 소리 내어 읽지 않게
        if style:
            try:
                return client.models.generate_content(
                    model=m, contents=text,
                    config=types.GenerateContentConfig(
                        system_instruction=style + " 아래 글만 그대로 읽고, 이 지시문은 절대 소리 내어 읽지 마세요.",
                        response_modalities=["AUDIO"], speech_config=voice))
            except Exception as e:
                if not any(k in str(e) for k in ("400", "INVALID_ARGUMENT")):
                    raise
        return client.models.generate_content(
            model=m, contents=text,
            config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=voice))

    r = _with_retry(call, [cfg["gemini_tts_model"], "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview",
                           "gemini-3.8-flash-lite-tts"])
    part = r.candidates[0].content.parts[0].inline_data
    data = part.data
    if isinstance(data, str):
        data = base64.b64decode(data)
    mime = (getattr(part, "mime_type", "") or "").lower()
    rate = 24000
    m_rate = re.search(r"rate=(\d+)", mime)
    if m_rate:
        rate = int(m_rate.group(1))
    if data[:4] == b"RIFF":           # 이미 wav 파일로 온 경우
        Path(out_wav).write_bytes(data)
        return out_wav
    if len(data) % 2:                 # 끝에 남는 반쪽 조각(잡음 원인) 버리기
        data = data[:-1]
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(data)
    return out_wav


# ---------------- 연결 시험 ----------------
def list_google_models(cfg):
    client = _gclient(cfg)
    return sorted(m.name.replace("models/", "") for m in client.models.list())
