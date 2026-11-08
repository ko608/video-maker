"""AI 호출 모음 — 영상 분석(제미나이), 대본(클로드), 이미지·영상·목소리(구글)."""
import base64, json, re, time, wave
from pathlib import Path


# ---------------- 공통 ----------------
def _gclient(cfg):
    from google import genai
    if not cfg.get("google_api_key"):
        raise RuntimeError("설정에서 구글 AI Studio 키를 넣어 주세요.")
    from google.genai import types
    # 구글 프로그램 새 버전은 기본 주소(v1)가 바뀌어 목소리 설정(speechConfig)을 못 씀 → v1beta로 고정
    return genai.Client(api_key=cfg["google_api_key"], http_options=types.HttpOptions(api_version="v1beta"))


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
    raw = m.group(0)
    try:
        return json.loads(raw)
    except Exception:
        # 대사 따옴표 등으로 JSON이 살짝 깨졌을 때 자동으로 고쳐서 읽기
        try:
            import json_repair
            fixed = json_repair.loads(raw)
            if isinstance(fixed, dict) and fixed:
                return fixed
        except Exception:
            pass
        raise


FALLBACK_TEXT = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-pro-latest", "gemini-3.1-pro-preview"]


def _with_retry(fn, models):
    """구글이 '사용자 많음(503)'일 때 잠깐 쉬었다 다시, 그래도 안 되면 다른 모델로."""
    last = None
    seen = []
    errs = {}
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
                errs[m] = msg[:160]
                if "402" in msg or "prepayment credits are depleted" in msg:
                    raise RuntimeError("구글 충전금(선불 크레딧)을 다 썼어요. ai.studio/projects 에서 충전해 주세요. "
                                       "충전 전에는 그림은 플로우에서 만들어 올리면 돼요(무료).")
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
                if any(k in msg for k in ("404", "NOT_FOUND", "no longer available", "not supported",
                                          "Unknown name", "Cannot find field")):
                    break  # 이 모델은 못 씀 → 바로 다음 모델로
                if not any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded", "500")):
                    raise
    raise RuntimeError("구글 모델을 쓸 수 없어요(바쁘거나 막힘). 몇 분 뒤 다시 눌러 주세요. 계속되면 이 화면을 Claude에게 보내 주세요.\n"
                       + "\n".join(f"• {k}: {v}" for k, v in errs.items()))


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
 "thumbnail": ["윗줄 10자 이내(흰 글씨, 상황)", "핵심 8자 이내(노란 큰 글씨, 반전·비밀)", ""],
 "music_mood": "배경음악 분위기 한 줄",
 "characters": "등장인물 외모 설명(영어, 모든 장면에서 같은 사람으로 보이게): 나이·머리·옷",
 "cuts": [
  {{"no": 1, "kind": "video", "line": "대사(따옴표 없이, 띄어쓰기 포함 15~50자 — 15~20자=4초, ~30자=6초, ~40자=8초, ~50자=10초 영상)", "speaker": "누가 누구에게", "speaker_en": "English: ONLY the person who speaks + look, e.g. the daughter-in-law (woman in her 30s, ponytail, beige cardigan)", "narration": "", "visual": "영어 장면 묘사: 말하는 사람 얼굴을 아주 가깝게(얼굴이 화면을 거의 채움, 눈동자까지 또렷), 상대는 뒤에 작고 흐리게"}},
  {{"no": 2, "kind": "image", "line": "", "speaker": "", "narration": "나레이션 90~130자", "visual": "영어: 나레이션 앞부분 내용에 맞는 화면", "shots": ["영어: 나레이션 가운데 내용에 맞는 화면(그 사람의 반응 얼굴 또는 그 순간의 음식·물건)", "영어: 나레이션 끝부분 내용에 맞는 화면"]}}
 ]
}}
cuts는 정확히 {ncuts}개, 규칙의 컷 구성을 따르세요.
line(대사)과 visual에는 돈·현금·병원·진료·수술·피·죽음·내놔·때리다·경찰 같은 격한 말을 쓰지 말고 부드러운 말로 바꿔 쓰세요(영상 도구가 거부함). 나레이션은 그대로 써도 됩니다.
그림+나레이션 컷(정지 화면)의 visual은 반드시 둘 중 하나로 쓰세요: ① 음식 클로즈업(김이 나는 국밥, 반찬, 손에 든 그릇 등 화면 가득) ② 인물 얼굴 아주 가까운 클로즈업(얼굴이 화면을 거의 채우고, 눈동자까지 보여 감정이 읽히게). 멀리서 사람 형체만 보이는 장면은 절대 쓰지 마세요. 옛날 이야기라도 인물은 단정하고 깔끔하게(옷·얼굴 깨끗하게) 묘사하세요. [화면 원칙] 대사(video) 컷 = 말하는 사람 얼굴 클로즈업. 나레이션(image) 컷 = 나레이션 내용에 딱 맞는 화면: 나레이션이 말하는 그 사람의 반응·표정, 또는 그 순간의 분위기(그 음식, 그 물건, 그 자리)를 보여 준다. 나레이션과 상관없는 엉뚱한 장면 금지. image 컷은 화면이 5~6초마다 바뀌어야 하므로 visual(나레이션 앞부분) + shots 2개(가운데, 끝부분) — 나레이션을 3등분해 그 순서대로 그 내용에 맞는 화면을 쓰세요. 각 컷의 visual은 대부분 인물 상반신이 가깝게 보이는 구도(medium close-up)로 쓰고, 넓게 멀리 찍는 구도는 피하세요. 각 컷의 visual에는 등장하는 주인공을 이름과 짧은 외모(예: Mrs. Kim — 60s, short grey hair, navy hanbok)로 매번 다시 적고, 모든 인물은 Korean으로 쓰세요. 이름 없는 새 인물(웨이터·손님 등)은 얼굴이 안 보이게 흐릿한 배경으로만. 각 컷의 visual에는 시대·장소를 구체적으로 쓰세요(예: 1970s shabby thatched-roof mountain tavern, small dim-free room with paper doors). 각 컷의 visual은 서로 확실히 달라야 합니다 — 장소·행동·카메라 구도(클로즈업/전신/손 클로즈업 등)·등장인물 조합을 컷마다 바꾸고, 같은 문장을 되풀이하지 마세요."""


LONG_SCRIPT_PROMPT = """당신은 시청자가 20~30분 동안 한 번도 넘기지 못하는 한국 '주말 드라마' 작가 겸 유튜브 오디오 드라마 작가입니다.
화면은 드라마 장면 그림 + 나레이션(인물 대사를 많이 포함)으로 진행되고, 장마다 가장 센 순간은 실제 대사 영상으로 보여 줍니다.
아래 [채널 규칙]을 지키세요.

[채널] {name}
[채널 규칙]
{rules}

[참고: 잘 된 영상 분석 — 구조와 리듬만 참고]
{analysis}

[대표님 요청 주제/메모 — 줄거리가 주어지면 그 줄거리를 살려 더 길고 긴장감 있게 키우세요]
{topic}

[긴장감 설계 — 반드시 지킬 것]
- 드라마처럼 '장면'으로 보여 준다: 설명보다 인물끼리 주고받는 대사로 갈등을 보여 준다. 나레이션 한 컷 안에 대사가 2~4번 오가게.
- 30초마다 새 정보·의심·오해·위기 중 하나가 나온다. 시청자가 '그래서 어떻게 되는데?'를 계속 묻게.
- 처음부터 숨겨진 비밀(정체·과거·약속) 하나를 깔고, 작은 단서(복선)를 장마다 하나씩 흘린다.
- 악역·오해하는 인물을 분명히 두고, 주인공이 점점 몰리다가 마지막 장에서 통쾌하게 뒤집힌다.
- 중간(가운데 장)에 첫 번째 큰 반전, 마지막 장에 가장 큰 반전과 감동.
- 장 마지막 컷은 반드시 절벽 끝에서 끊는다: '그 순간, 문이 열렸습니다.' '봉투 안에 들어 있던 건… 돈이 아니었습니다.'
- 뻔한 '고생 끝에 성공' 흐름 금지.

아래 JSON 하나만 출력하세요. 다른 말은 쓰지 마세요.
{{
 "title_options": ["제목 후보 3개 — 첫 대사 인용 + 반전 암시, 예: “남은 국물만 주세요” 그 손님의 정체…"],
 "description": "유튜브 설명 4~6줄 (줄바꿈은 \\n). 마지막 줄은 '※ 이 영상은 창작 드라마이며 등장인물과 사건은 모두 가상입니다.'",
 "tags": "쉼표로 구분한 태그 한 줄 (15개 안팎)",
 "pinned_comment": "고정 댓글 한 줄 (시청자 경험을 묻는 질문)",
 "thumbnail": ["윗줄 8자 이내(상황)", "핵심 한마디 6~8자(가장 궁금한 대사·반전)", ""],
 "music_mood": "배경음악 분위기 한 줄",
 "characters": "등장인물 외모 설명(영어, 모든 장면에서 같은 사람으로 보이게): 이름·나이·머리·옷 (깔끔하고 단정하게)",
 "opening": [
  {{"line": "첫 장면 대사(15~30자, 갈등이 바로 터지는 말)", "speaker": "누가 누구에게", "speaker_en": "English: ONLY the person who speaks + look", "visual": "영어 드라마 장면: 말하는 사람 얼굴을 아주 가깝게(얼굴이 화면을 거의 채움, 눈동자까지 또렷)"}}
 ],
 "chapters": [
  {{"title": "장 제목(짧게)", "cuts": [
    {{"kind": "image", "narration": "나레이션 150~230자. 인물 대사를 “둥근 따옴표”로 2~4번 넣어 드라마처럼(곧은 따옴표는 절대 쓰지 말 것)", "visual": "영어: 나레이션 앞부분 내용에 맞는 드라마 화면", "shots": ["영어: 나레이션 가운데에 맞는 화면(반응 얼굴·분위기)", "영어: 나레이션 끝부분에 맞는 화면"]}},
    {{"kind": "video", "line": "이 장에서 가장 센 순간의 대사(15~40자)", "speaker": "누가 누구에게", "speaker_en": "English: ONLY the person who speaks + look", "visual": "영어 드라마 장면: 말하는 사람 얼굴을 아주 가깝게(얼굴이 화면을 거의 채움, 눈동자까지 또렷)"}}
  ]}}
 ]
}}
opening은 영상 맨 앞 '대사 영상' {nopen}개(각 4~6초) — 이야기의 가장 강렬한 갈등 순간을 먼저 보여 주고, 1장 나레이션이 '모든 일은 그날 아침부터 시작됐습니다'처럼 이어받게.
chapters는 {nchap}개, 전체 cuts는 {ncuts}개 안팎(장마다 비슷하게). 각 장에 kind "video" 컷을 정확히 1개 — 그 장의 가장 센 순간에 넣는다. 나머지는 kind "image".
image 컷 나레이션은 150~230자를 꼭 지켜 전체 길이를 맞추세요.
대사(video) 컷은 말하는 사람 얼굴 클로즈업, 나레이션(image) 컷은 나레이션 내용에 딱 맞는 상대방의 반응이나 그 순간의 분위기(음식·물건·자리) — 엉뚱한 장면 금지. visual은 영화처럼 멀고 웅장한 화면이 아니라 '한국 TV 드라마' 화면처럼: 인물 중심, 대화하는 두 사람(투샷), 어깨 너머로 상대 얼굴(오버숄더), 말 듣는 사람의 반응 클로즈업, 음식 클로즈업. 멀리서 찍은 장면·풍경만 있는 장면 금지.
대사(line)와 visual에는 돈·병원·내놔 같은 격한 말 대신 부드러운 말을 쓰세요. 컷마다 visual은 서로 달라야 합니다."""


def _write_long(cfg, channel, analysis, topic):
    prompt = LONG_SCRIPT_PROMPT.format(
        name=channel["name"], rules="\n".join("- " + r for r in channel["rules"]),
        analysis=analysis or "(분석 없음)", topic=topic or "(자유 주제)",
        ncuts=channel["cuts"], nchap=channel.get("chapters", 5), nopen=int(channel.get("opening_videos", 3)))
    data = _json_from(_claude(cfg, prompt, max_tokens=min(64000, int(channel['cuts']) * 450 + 3000)))
    cuts, n = [], 0
    for o in data.get("opening", [])[:int(channel.get("opening_videos", 3))]:
        n += 1
        cuts.append({"no": n, "kind": "video", "chapter": "", "line": o.get("line", ""), "speaker": o.get("speaker", ""),
                     "speaker_en": o.get("speaker_en", ""), "narration": "", "visual": o.get("visual", "")})
    first_chapter = True
    for ch in data.get("chapters", []):
        for i, c in enumerate(ch.get("cuts", [])):
            n += 1
            kind = "video" if c.get("kind") == "video" and c.get("line") else "image"
            cuts.append({"no": n, "kind": kind, "chapter": ch.get("title", "") if i == 0 else "",
                         "line": c.get("line", "") if kind == "video" else "",
                         "speaker": c.get("speaker", "") if kind == "video" else "",
                         "speaker_en": c.get("speaker_en", "") if kind == "video" else "",
                         "narration": c.get("narration", "") if kind == "image" else "",
                         "visual": c.get("visual", ""), "shots": c.get("shots", []) if kind == "image" else []})
    data["cuts"] = cuts
    data.pop("chapters", None)
    data.pop("opening", None)
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
    # 플로우가 거부하는 격한 단어를 대사·장면 묘사에서 부드럽게 (나레이션은 그대로)
    from .soften import soften
    for c in data.get("cuts", []):
        c["line"] = soften(c.get("line", ""))
        c["visual"] = soften(c.get("visual", ""))
        c["shots"] = [soften(x) for x in (c.get("shots") or [])][:2]
    return data


def video_seconds_for(line):
    """대사 글자 수 → 영상 길이 (15~20자 4초, ~30자 6초, ~40자 8초, 그 이상 10초)"""
    n = len((line or "").strip())
    return 4 if n <= 20 else 6 if n <= 30 else 8 if n <= 40 else 10


# ---------------- 2-0. 대표님이 쓴 대본 그대로 → 컷 나누기 ----------------
def script_from_text(cfg, channel, title, text):
    fmt = channel.get("format", "shorts")
    prompt = f"""아래는 대표님이 직접 쓴 유튜브 영상 대본입니다. 글(대사·나레이션)은 한 글자도 바꾸지 말고 그대로 쓰면서,
영상 자판기용 컷으로 나눠 주세요. 대사가 길면 컷을 여러 개로 나눠도 되지만 내용은 그대로 둡니다.

[채널] {channel['name']}  (형식: {'가로 롱폼' if fmt == 'longform' else '세로 쇼츠'})
[화면 규칙]
{chr(10).join('- ' + r for r in channel['rules'] if '컷' in r or '화면' in r or 'visual' in r or '클로즈업' in r)}
- 대사 컷(kind "video") = 말하는 사람 얼굴 아주 가까운 클로즈업(눈동자까지). line은 대본 그대로 — 영상 길이는 글자 수에 맞춰 4·6·8·10초(50자 넘으면 컷을 나눔).
- 나레이션 컷(kind "image") = 나레이션 내용에 맞는 화면. 나레이션이 12초보다 길면 컷을 나눈다. visual(앞부분)+shots 2개(가운데·끝부분).
- 대사·장면 묘사에는 돈·병원·내놔 같은 격한 말 대신 부드러운 말 (나레이션 글은 그대로).

[제목] {title or '(없음 — 대본에 맞게 3개 만들어 주세요)'}
[대본]
{text}

아래 JSON 하나만 출력하세요.
{{"title_options": ["제목이 있으면 그 제목을 첫 번째로, 나머지 2개는 비슷한 후보"],
 "description": "유튜브 설명 3~5줄 (줄바꿈은 \\n)", "tags": "쉼표 구분 태그 15개 안팎", "pinned_comment": "고정 댓글 한 줄",
 "thumbnail": ["윗줄 10자 이내", "핵심 8자 이내", ""], "music_mood": "배경음악 분위기",
 "characters": "등장인물 외모 설명(영어, 깔끔하게)",
 "cuts": [
  {{"no": 1, "kind": "video", "line": "대본의 대사 그대로", "speaker": "누가 누구에게", "speaker_en": "English: the speaker + look", "narration": "", "visual": "영어 장면"}},
  {{"no": 2, "kind": "image", "line": "", "speaker": "", "narration": "대본의 나레이션 그대로", "visual": "영어 장면", "shots": ["영어", "영어"]}}
 ]}}"""
    data = _json_from(_claude(cfg, prompt, max_tokens=min(64000, 4000 + len(text) * 6)))
    for i, c in enumerate(data.get("cuts", []), 1):
        c["no"] = i
        c.setdefault("line", ""); c.setdefault("narration", ""); c.setdefault("speaker", "")
    up = channel.get("upload", {})
    if up.get("description_tail") and up["description_tail"] not in data.get("description", ""):
        data["description"] = data.get("description", "").rstrip() + "\n" + up["description_tail"]
    if up.get("base_tags"):
        have = [t.strip() for t in data.get("tags", "").split(",") if t.strip()]
        data["tags"] = ",".join(have + [t for t in up["base_tags"].split(",") if t not in have])
    data["format"] = fmt
    from .soften import soften
    for c in data.get("cuts", []):
        c["visual"] = soften(c.get("visual", ""))   # 대사는 대표님 글 그대로 (플로우 문구에서만 순화됨)
        c["shots"] = [soften(x) for x in (c.get("shots") or [])][:2]
    return data


def add_shots(cfg, data):
    """이미 만든 대본의 그림 컷마다 '추가 화면 2개'(나레이션 가운데·끝부분에 맞는 장면)를 써 줌"""
    items = [{"no": c["no"], "narration": c.get("narration", ""), "visual": c.get("visual", "")}
             for c in data.get("cuts", []) if c.get("kind") == "image"]
    prompt = f"""아래 영상 대본의 그림 컷마다, 나레이션을 3등분했을 때 가운데·끝부분 내용에 딱 맞는 화면 묘사 2개를 영어로 써 주세요.
첫 화면(visual)은 이미 있으니 그 다음 두 화면만. 엉뚱한 장면 금지 — 나레이션이 말하는 그 사람의 반응 얼굴, 또는 그 순간의 음식·물건·자리.
얼굴은 아주 가깝게(눈동자까지), 멀리서 찍은 장면 금지, 한 장에 한 장면.
등장인물: {data.get('characters','')}
컷 목록: {json.dumps(items, ensure_ascii=False)}
JSON 하나만 출력: {{"컷번호": ["가운데 화면", "끝 화면"], ...}}"""
    out = _json_from(_claude(cfg, prompt, max_tokens=4000))
    return {str(k): v for k, v in out.items() if isinstance(v, list)}


def match_images(cfg, slots, images):
    """올린 그림들을 보고 어느 자리(컷)에 맞는지 AI가 짝지어 줌.
    slots: {"2": "장면 설명", "2-2": "...", ...}, images: [bytes, ...] → ["2-2", "4", ...] (같은 길이)"""
    import anthropic, io
    from PIL import Image
    c = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    content = [{"type": "text", "text":
                "아래 '자리 목록'의 장면 설명과, 뒤에 오는 그림들을 비교해서 각 그림이 어느 자리에 가장 맞는지 짝지어 주세요. "
                "한 자리에 그림 하나씩, 맞는 자리가 없으면 \"없음\". 인물 여러 명이 나란히 선 기준 그림은 \"없음\".\n"
                "자리 목록:\n" + "\n".join(f"- {k}: {v}" for k, v in slots.items())}]
    for i, b in enumerate(images, 1):
        im = Image.open(io.BytesIO(b)).convert("RGB"); im.thumbnail((384, 384))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=70)
        content.append({"type": "text", "text": f"그림 {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(buf.getvalue()).decode()}})
    content.append({"type": "text", "text": 'JSON 하나만 출력: {"1": "자리", "2": "자리", ...} (그림 번호 → 자리 이름)'})
    r = c.messages.create(model=cfg["claude_model"], max_tokens=2000, messages=[{"role": "user", "content": content}])
    out = _json_from("".join(b.text for b in r.content if getattr(b, "type", "") == "text"))
    res, used = [], set()
    for i in range(1, len(images) + 1):
        v = str(out.get(str(i), "없음"))
        if v in slots and v not in used:
            res.append(v); used.add(v)
        else:
            res.append("넣지 않음")
    return res


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
def _tts_interactions(client, cfg, model, text, out_wav):
    """구글 새 목소리 방식(Interactions API, gemini-3.8 TTS) — 말투는 '지시'가 아니라 꼬리표로 붙여서 읽지 않음"""
    content = {"type": "text", "text": text}
    style = (cfg.get("tts_style") or "").strip()
    if style:
        content["annotations"] = [{"type": "speech_metadata", "style": style}]
    it = client.interactions.create(
        model=model,
        input=[{"type": "user_input", "content": [content]}],
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": cfg.get("tts_voice", "Kore")}]},
    )
    data = it.output_audio.data
    data = base64.b64decode(data) if isinstance(data, str) else data
    if data[:4] != b"RIFF":           # 혹시 날것 소리로 오면 wav로 감싸기
        if len(data) % 2:
            data = data[:-1]
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(data)
    else:
        Path(out_wav).write_bytes(data)
    return out_wav


def make_tts(cfg, text, out_wav):
    from google.genai import types
    client = _gclient(cfg)
    # 1순위: 새 방식 (3.8 TTS)
    if hasattr(client, "interactions"):
        try:
            return _with_retry(lambda m: _tts_interactions(client, cfg, m, text, out_wav),
                               [m for m in [cfg.get("gemini_tts_model"), "gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
                                if m and "3.8" in m] or ["gemini-3.8-flash-tts"])
        except Exception as e:
            first_err = str(e)[:400]
            if "한도" in first_err:
                raise
    else:
        first_err = ""
    # 2순위: 예전 방식 (generate_content) — 3.1 미리보기 목소리 등
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

    try:
        r = _with_retry(call, ["gemini-3.1-flash-tts-preview", "gemini-2.5-pro-preview-tts",
                               cfg["gemini_tts_model"], "gemini-3.8-flash-tts"])
    except Exception as e:
        raise RuntimeError(f"[목소리 오류] 새 방식: {first_err}\n예전 방식: {str(e)[:500]}")
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
