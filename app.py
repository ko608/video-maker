"""영상 자판기 — 대표님 전용 영상 자동 제작 프로그램 (쇼츠 1단계)
실행: 실행하기.bat 더블클릭 → 인터넷 창이 열립니다."""
import json, os, re, shutil, subprocess, sys, time
from datetime import datetime
from pathlib import Path

import streamlit as st
from vm.soften import soften
_T0 = time.time()

sys.path.insert(0, str(Path(__file__).parent))
from vm import config, ai, assemble  # noqa: E402

st.set_page_config(page_title="영상 자판기", page_icon="🎬", layout="wide")
cfg = config.load()

st.markdown("""<style>
.stApp{background:linear-gradient(180deg,#FFF6D6 0%,#FFE6EE 100%);}
h1,h2,h3{color:#5E1813}
.stButton>button{font-size:18px;font-weight:700}
</style>""", unsafe_allow_html=True)
st.title("🎬 영상 자판기")
@st.cache_resource(show_spinner=False)
def _version():
    try:
        import subprocess
        return subprocess.run(["git", "log", "-1", "--format=%cd", "--date=format:%m월 %d일 %H:%M"],
                              cwd=str(config.ROOT), capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return "?"
st.caption(f"잘 되는 영상 링크 → 분석 → 우리식 대본 → 재료 → 완성 영상까지  ·  버전: {_version()}")


# ---------------- 도우미 ----------------
@st.cache_data(show_spinner=False, max_entries=300)
def _small(path_str, mtime, width=360):
    """미리보기용 작은 그림 (원본 2K를 그대로 띄우면 컴퓨터가 무거워짐)"""
    from PIL import Image as _I
    import io
    im = _I.open(path_str).convert("RGB")
    im.thumbnail((width, width * 2))
    b = io.BytesIO(); im.save(b, "JPEG", quality=80)
    return b.getvalue()


@st.cache_data(show_spinner=False, max_entries=800)
def _img_sig(name, size, data):
    """그림 지문: 작은 흑백 모양(dHash) + 색 분포"""
    from PIL import Image as _I
    import io
    im = _I.open(io.BytesIO(data)).convert("RGB")
    g = im.convert("L").resize((17, 16))
    px = list(g.getdata())
    bits = [px[r * 17 + c] > px[r * 17 + c + 1] for r in range(16) for c in range(16)]
    small = im.resize((8, 8))
    col = [v // 32 for p_ in small.getdata() for v in p_]
    return bits, col, im.size[0] / max(1, im.size[1])


def _similar(a, b):
    bits_a, col_a, r_a = a
    bits_b, col_b, r_b = b
    if abs(r_a - r_b) > 0.05:
        return False
    ham = sum(x != y for x, y in zip(bits_a, bits_b))          # 256비트 중 다른 칸 수
    cdiff = sum(abs(x - y) for x, y in zip(col_a, col_b)) / len(col_a)
    return ham < 70 and cdiff < 0.9


@st.cache_data(show_spinner=False, max_entries=400)
def _upload_thumb(name, size, data):
    from PIL import Image as _I
    import io
    im = _I.open(io.BytesIO(data)).convert("RGB"); im.thumbnail((140, 250))
    b = io.BytesIO(); im.save(b, "JPEG", quality=70)
    return b.getvalue()


def show_small(box, path, caption=None):
    path = Path(path)
    box.image(_small(str(path), path.stat().st_mtime), caption=caption, width="stretch")


def new_upload(key, up):
    """같은 파일을 화면이 바뀔 때마다 다시 저장하지 않게"""
    if up is None:
        return False
    sig = f"{up.name}:{up.size}"
    if st.session_state.get("_up_" + key) == sig:
        return False
    st.session_state["_up_" + key] = sig
    return True


def slot_from_name(name):
    """파일 이름에서 자리 읽기: c02 → 2, c02-2 / c02_2 → '2-2', v03 → 영상 3 (못 찾으면 None)"""
    import re as _re
    m = _re.search(r"(?<![a-z0-9])([cv])[_\- ]?(\d{1,3})(?:[_\- ](\d)(?![0-9a-z]))?(?![0-9])", Path(name).stem.lower())   # 뒤에 붙는 플로우 임의 글자(c05_3d69…)는 무시
    if not m:
        return None
    no = int(m.group(2))
    if m.group(1) == "v":
        return no
    return f"{no}-{m.group(3)}" if m.group(3) and m.group(3) in ("2", "3") else no


def seq_slots(data):
    """그림 자리를 차례대로: [ '2', '2-2', '2-3', '4', … ] → 그림 번호 c01, c02, c03, c04 …"""
    out = []
    for c in data.get("cuts", []):
        if c["kind"] != "image":
            continue
        n_ = int(c["no"])
        out.append(str(n_))
        out += [f"{n_}-{k_}" for k_ in range(2, 2 + len(c.get("shots") or []))]
    return out


def seq_no(data, slot):
    """자리('4-2') → 그림 번호(5) — 차례 번호 방식일 때만"""
    try:
        return seq_slots(data).index(str(slot)) + 1
    except ValueError:
        return None


def slot_of(name, data):
    """파일 이름 → 자리. 차례 번호 방식(c01, c02 …)이면 번호를 자리로 바꿔 줌"""
    sl = slot_from_name(name)
    if sl is None or data.get("naming") != "seq":
        return sl
    import re as _re
    m_ = _re.search(r"(?<![a-z0-9])([cv])[_\- ]?\d", Path(name).stem.lower())
    if isinstance(sl, int) and m_ and m_.group(1) == "c":
        lst = seq_slots(data)
        if 1 <= sl <= len(lst):
            v = lst[sl - 1]
            return int(v) if "-" not in v else v
        return None
    return sl


def channels():
    out = {}
    for f in sorted(config.CHANNELS.glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        out[c["name"]] = c
    return out


def projects():
    return sorted([p for p in config.PROJECTS.iterdir() if (p / "script.json").exists() and not p.name.startswith("_")],
                  reverse=True)


def load_script(p):
    return json.loads((p / "script.json").read_text(encoding="utf-8"))


def save_script(p, data):
    (p / "script.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "", s)[:30].strip() or "제목없음"


def open_folder(p):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)])
    except Exception:
        pass


def cut_file(p, no):
    for f in sorted((p / "재료").glob(f"{no:02d}.*")):
        return f
    return None


LOOK_EN = ("All people look about 5 to 10 years younger than their stated age, good-looking but natural, neat and tidy: "
           "clean well-groomed hair, smooth clear skin, fresh clean well-fitting clothes with no stains or wrinkles. "
           "The face and its emotion are the subject: frame tightly so the face fills most of the picture; "
           "the background is real but softly out of focus (shallow depth of field), bright, clean and simple, "
           "while the people stay crisp and sharp")
LOOK_KO = ("인물은 설정 나이보다 5~10살 젊어 보이게, 단정하고 깔끔하게(정돈된 머리, 맑은 피부, 얼룩·구김 없는 깨끗한 옷). "
           "주인공은 얼굴 표정과 감정 — 얼굴이 화면 대부분을 채우게 가깝게, 배경은 밝고 깨끗하게, 카메라 초점이 인물에 맞아 뒤는 부드럽게 흐려진 느낌(그림처럼 뭉개지 말 것), 인물만 또렷하게")
ART_STYLES = {
    "반실사 일러스트": ("Realistic semi-real style, about 75 percent realistic: real human facial structure and proportions, "
                  "realistic eyes, nose and skin, like a bright, polished Korean TV drama still with a light beauty retouch — realistic but clean: skin, clothes and background "
                  "are smooth and tidy, not painterly, no brush strokes. NOT anime, NOT cartoon, NOT webtoon, no big anime eyes, no outlines; "
                  "the person is crisp and sharp with good contrast, background evenly lit — no glow, no bloom, no haze",
                  "밝고 깨끗한 한국 TV 드라마 장면 같은 반실사(얼굴·피부는 실제 사람처럼 자연스럽게, 가벼운 뽀샵 보정으로 매끈하고 깔끔하게 — 붓 그림·일러스트 느낌 금지, 거칠고 칙칙한 실사 사진도 금지), "
                  "애니메이션·만화·웹툰 느낌 금지, 인물은 윤곽이 또렷하고 선명하게, 배경 빛 번짐·뿌연 느낌 없이"),
    "깔끔한 드라마 사진풍": ("Clean polished Korean TV drama still photo: smooth even skin, no grit, no film grain, tidy simple background",
                     "깔끔한 드라마 사진풍(매끈한 피부, 거친 질감·지저분한 배경 없이)"),
    "웹툰풍": ("Korean webtoon art style: clean line art, bright flat colors, expressive faces, simple backgrounds",
            "한국 웹툰 그림체(깔끔한 선, 밝은 색, 표정이 풍부하게, 단순한 배경)"),
}


def art_style(cfg):
    en, ko = ART_STYLES.get(cfg.get("art_style", "반실사 일러스트"), ART_STYLES["반실사 일러스트"])
    return en + ". " + LOOK_EN, ko + ". " + LOOK_KO


EXTRA = ["b", "c"]   # 그림 컷의 두 번째·세 번째 화면 파일: 02_b.png, 02_c.png


def extra_files(p, no):
    out = []
    for t in EXTRA:
        f = sorted((p / "재료").glob(f"{int(no):02d}_{t}.*"))
        if f:
            out.append(f[0])
    return out


def scene_pics(p):
    """썸네일 후보 그림 (기준 그림 제외) — 컷 그림·추가 화면·첫 장면·썸네일 전용"""
    return sorted([x for x in (p / "재료").iterdir() if x.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                   and "기준" not in x.name], key=lambda x: x.name)


def make_thumbs(p, data, fmt, picks=None):
    """썸네일 A·B — 둘 다 글씨는 아래, 서로 다른 핵심 장면 2곳"""
    assemble.set_format(fmt)
    pics = scene_pics(p)
    if picks:
        chosen = [x for x in picks if x]
    else:
        chosen = [Path(x) for x in data.get("thumb_scenes", []) if Path(x).exists()]
        if len(chosen) < 2:
            chosen = []
            try:     # AI가 대본을 보고 핵심 장면 2곳(컷)을 고름 — 몇 원
                for no in ai.pick_key_scenes(cfg, data):
                    f_ = cut_file(p, no)
                    if f_ is None or f_.suffix.lower() in (".mp4", ".mov"):
                        st_ = list((p / "재료").glob(f"{no:02d}_첫장면.*"))
                        f_ = st_[0] if st_ else None
                    if f_ is not None:
                        chosen.append(f_)
            except Exception:
                pass
        special = [x for x in pics if x.stem == "썸네일전용"]
        if special and special[0] not in chosen:
            chosen = special + chosen
        scenes = [x for x in pics if x.stem != "썸네일전용"]
        for x in [scenes[len(scenes) * 2 // 3] if scenes else None, scenes[0] if scenes else None]:
            if len(chosen) >= 2:
                break
            if x is not None and x not in chosen:
                chosen.append(x)
    chosen = (chosen + chosen[:1])[:2] if chosen else [None, None]
    data["thumb_scenes"] = [str(x) for x in chosen if x]
    save_script(p, data)
    return assemble.thumbnails(p, data.get("thumbnail", []), chosen, cfg)


def thumb_prompt(data, fmt, style):
    """썸네일 전용 그림: 주인공 얼굴 크게, 밝게, 아래쪽은 글씨 자리"""
    hook = " / ".join(x for x in data.get("thumbnail", []) if x)
    where = ("face in the upper half of the frame, lower 40% kept simple for big text"
             if fmt != "longform" else "face on the right half, left side and bottom kept simple for big text")
    return (f"YouTube thumbnail photo. Close-up from the chest up of the MAIN CHARACTER: the face is very large, "
            f"forehead to chin about 50 percent of the frame height, eyes sharp with catchlights so the emotion reads in the pupils, face centered horizontally (never cut off at the edge), {where}. "
            "Other characters may appear small and softly blurred behind. "
            f"Strong clear emotion that fits this hook: '{hook}' (shock, tears, anger or surprise). "
            "Very bright high-key lighting, bright clean background softly blurred, vivid but natural colors, "
            "sharp eyes. Ordinary everyday Korean person with a natural face. " + art_style(cfg)[0] + ". "
            f"Characters: {data.get('characters','')}. Keep the setting's era as in the story. "
            "One single image, no collage, no text, no watermark.")


def make_thumb_image(p, data, fmt):
    ch = {c_["id"]: c_ for c_ in channels().values()}.get(data.get("channel"), {})
    aspect = "16:9" if fmt == "longform" else "9:16"
    ref = p / "재료" / "인물기준.png"
    out = p / "재료" / "썸네일전용.png"
    ai.make_image(cfg, thumb_prompt(data, fmt, ch.get("visual_style", "")), out, aspect=aspect,
                  ref=ref if ref.exists() else None)
    return out


class Log:
    def __init__(self):
        self.box = st.empty(); self.lines = []
    def __call__(self, s):
        self.lines.append(str(s)); self.box.code("\n".join(self.lines[-14:]))


# 탭 대신 고르기 버튼 — 고른 화면만 그려서 가볍게 (st.tabs는 네 화면을 매번 다 그림)
PAGES = ["① 분석·대본", "② 재료 만들기", "③ 완성 영상", "⚙️ 설정"]
page = st.radio("화면", PAGES, horizontal=True, label_visibility="collapsed", key="page")
st.divider()
_T1 = time.time()

# ================= ① 분석·대본 =================
if page == "① 분석·대본":
    chs = channels()
    ch_name = st.selectbox("우리 채널", list(chs))
    ch = dict(chs[ch_name])
    if ch.get("format") == "longform":
        mins = st.select_slider("⏱ 영상 길이 (분)", options=[5, 8, 10, 15, 20, 25, 30], value=10,
                                help="길수록 그림·목소리 개수가 늘어 비용과 만드는 시간이 늘어요")
        ch["cuts"] = max(10, round(mins * 60 / 34))          # 장면 1개 ≈ 30~35초 (나레이션 200~260자)
        ch["minutes"] = mins
        ch["chapters"] = max(3, min(10, mins // 3 + 1))
        st.caption(f"→ 장면 약 {ch['cuts']}개, {ch['chapters']}개 장으로 써요. "
                   f"(자동 그림이면 대략 {ch['cuts'] * 70:,}~{ch['cuts'] * 130:,}원)")
    else:
        mode = st.radio("🎬 구성", ["올영상 (전부 대사 영상)", "섞기 (1·3·5 영상 + 그림·나레이션)"], horizontal=True,
                        help="올영상: 인물들이 대사를 주고받는 영상 컷으로만 — 플로우 영상 개수가 늘어요")
        secs = st.radio("⏱ 쇼츠 길이", ["약 60초", "약 90초", "약 120초"], horizontal=True)
        allv = mode.startswith("올영상")
        sec_n = {"약 60초": 60, "약 90초": 90, "약 120초": 120}[secs]
        n = round(sec_n / 5) if allv else sec_n // 10
        if allv:
            ch["cuts"] = n
            twist = round(n * 2 / 3)
            ch["rules"] = list(ch["rules"]) + [
                f"[올영상 구성 — 위 규칙보다 우선] 전체 {n}컷 모두 kind \"video\"(대사 영상). 그림+나레이션 컷과 narration은 쓰지 않는다 — 이야기는 오직 인물들의 대사로만 전한다. "
                "대사는 컷마다 15~30자(4~6초), 결정적인 한마디만 40자 이내(8초). 두 사람 이상이 주고받게 — 말 → 상대의 반응 대사 → 다시 말, 한 사람이 연달아 길게 말하지 않는다. "
                "1컷은 갈등이 바로 터지는 첫마디(설명 금지). 이야기의 열쇠 물건·음식(봉투, 국밥, 앞치마 등)이 대사와 화면에 반복해 나온다. "
                f"작은 반전·의문은 3~4컷마다, 큰 반전은 {twist}컷 무렵 한마디로 터뜨린다. 마지막 컷은 여운이 남는 한마디 대사로 끝낸다(채널 마무리 나레이션 규칙은 무시). "
                "화면은 말하는 사람 얼굴 클로즈업을 기본으로, 3~4컷에 한 번은 두 사람이 함께 보이는 허리 위 투샷이나 어깨 너머 샷, 반전 컷은 듣는 사람의 놀란 얼굴. "
                "characters에는 인물마다 끝까지 바뀌지 않는 대표 옷·색·소품을 정해 적고(예: 연보라 니트, 남색 체크 셔츠), 반전 뒤에는 옷이나 소품 하나를 바꿔 변화를 보여 줄 수 있다(예: 남편이 앞치마를 두름)."]
        elif n != ch.get("cuts", 6):
            ch["cuts"] = n
            twist = round(n * 2 / 3)
            ch["rules"] = list(ch["rules"]) + [
                f"[이번 길이 우선 적용] 전체 {n}컷: 홀수 컷(1·3·5…)은 대사 영상(4~10초, 대사 길이에 맞춤), 짝수 컷은 이미지 + 나레이션(90~130자). "
                f"위 규칙의 '6컷'·'4컷 반전'은 무시하고, 큰 반전은 {twist}컷 무렵, 작은 반전·의문은 3컷마다 넣는다. "
                "마지막 컷 나레이션 마무리 문장 규칙은 그대로 지킨다."]
    url = st.text_input("잘 되는 영상 유튜브 링크 (없으면 비워 두고 아래 주제만 써도 됩니다)")
    # ---- 💡 한 단어 → 주제 후보 3~4개 뽑기 ----
    k1, k2 = st.columns([3, 1])
    kw = k1.text_input("💡 한 단어만 넣어도 주제를 뽑아 드려요", placeholder="예: 국밥, 보험, 반찬, 진상, 김장")
    k2.write(""); k2.write("")
    if k2.button("주제 뽑기", width="stretch"):
        try:
            with st.spinner("주제 후보 만드는 중…"):
                st.session_state["ideas"] = ai.suggest_topics(cfg, ch, kw)
        except Exception as e:
            st.error(f"오류: {e}")
    ideas = st.session_state.get("ideas") or []
    if ideas:
        pick = st.radio("마음에 드는 주제를 고르세요 (고르면 아래 '주제·메모' 칸에 들어가요)",
                        ["(고르지 않음)"] + [f"{i['title']} — {i['twist']}" for i in ideas], key="idea_pick")
        if pick != "(고르지 않음)" and st.session_state.get("_last_pick") != pick:
            st.session_state["_last_pick"] = pick
            it = ideas[[f"{i['title']} — {i['twist']}" for i in ideas].index(pick)]
            st.session_state["topic_box"] = f"{it['title']}. 반전: {it['twist']}"
    topic = st.text_area("주제·메모 (선택) — 예전에 만든 이야기 줄거리를 통째로 붙여 넣어도 돼요 (롱폼으로 키워 줘요)",
                         placeholder="예: 시어머니가 며느리 몰래 들어 둔 보험. / 또는 붕어빵 시리즈 줄거리 전체",
                         height=160, key="topic_box")

    c1, c2 = st.columns(2)
    if c1.button("🔍 분석하고 대본 만들기", type="primary", width="stretch"):
        log = Log()
        try:
            analysis = ""
            if url.strip():
                log("영상 분석 중… (1~2분)")
                analysis = ai.analyze_youtube(cfg, url, topic)
                st.session_state["analysis"] = analysis
            log("우리식 대본 쓰는 중… (1분 안팎)")
            st.session_state["draft"] = ai.write_script(cfg, ch, analysis, topic)
            st.session_state["draft_channel"] = ch["id"]
            log("완료! 아래에서 확인하고 고친 뒤 저장하세요.")
        except Exception as e:
            st.error(f"오류: {e}")
    if c2.button("🔁 대본만 다시 쓰기", width="stretch"):
        try:
            with st.spinner("다시 쓰는 중…"):
                st.session_state["draft"] = ai.write_script(cfg, ch, st.session_state.get("analysis", ""), topic)
        except Exception as e:
            st.error(f"오류: {e}")

    with st.expander("📝 내 제목·대본 통째로 넣기 (우리 형식으로 다듬기 / 글 그대로)"):
        my_title = st.text_input("제목 (있으면)", key="my_title")
        my_text = st.text_area("대본 전체 붙여 넣기 — 대사·나레이션 그대로 쓰여요",
                               height=260, key="my_text",
                               placeholder="예)\n아들: 엄마 생신인데 또 엄마가 계산해?\n(나레이션) 그날 저녁, 식당 안은 조용해졌습니다…")
        mode_ = st.radio("어떻게 쓸까요?", ["우리 형식으로 다듬기 (줄거리·핵심 대사는 살리고, 컷 수·길이·대사 길이·마무리 문장을 채널 규칙에 맞게)",
                                         "글 그대로 쓰기 (한 글자도 안 바꾸고 컷만 나누기)"], key="my_mode")
        if st.button("✂️ 이 대본으로 만들기", type="primary", disabled=not my_text.strip()):
            try:
                with st.spinner("만드는 중… (1분 안팎)"):
                    if mode_.startswith("우리 형식"):
                        topic_ = ("[원본 대본 — 이 이야기의 줄거리·인물·핵심 대사·반전은 그대로 살리고, "
                                  "채널 규칙(컷 수, 컷별 구성, 대사 길이, 나레이션 길이, 화면 원칙, 마지막 문장)에 맞게 다시 쓸 것. "
                                  "원본 대사 중 좋은 말은 최대한 살려 쓴다]\n"
                                  + (f"제목: {my_title}\n" if my_title else "") + my_text)
                        d_ = ai.write_script(cfg, ch, "", topic_)
                        if my_title:
                            d_["title_options"] = [my_title] + [t for t in d_.get("title_options", []) if t != my_title][:2]
                        st.session_state["draft"] = d_
                    else:
                        st.session_state["draft"] = ai.script_from_text(cfg, ch, my_title, my_text)
                    st.session_state["draft_channel"] = ch["id"]
                st.success("완료! 아래에서 확인하고 고친 뒤 저장하세요.")
            except Exception as e:
                st.error(f"오류: {e}")

    if st.session_state.get("analysis"):
        with st.expander("📊 영상 분석 보고서 (장점·단점·적용점)", expanded=False):
            st.markdown(st.session_state["analysis"])

    d = st.session_state.get("draft")
    if d:
        st.subheader("✍️ 대본 (칸을 눌러 바로 고칠 수 있어요)")
        cuts = st.data_editor(d["cuts"], num_rows="fixed", width="stretch",
                              column_config={"no": "컷", "chapter": "장", "kind": "종류(video/image)", "line": "대사",
                                             "speaker": "누가", "speaker_en": "말하는 사람(영어)", "narration": "나레이션", "visual": "장면 묘사(영어)"})
        a, b = st.columns(2)
        title = a.selectbox("제목", d.get("title_options", ["제목"]))
        tags = b.text_input("태그 (한 줄)", d.get("tags", ""))
        desc = st.text_area("설명", d.get("description", ""), height=110)
        pinned = st.text_input("고정 댓글", d.get("pinned_comment", ""))
        th = st.text_input("썸네일 문구 (| 로 3줄 구분)", " | ".join(d.get("thumbnail", [])))
        chars = st.text_input("등장인물 외모 (영어 — 모든 장면에 똑같이 들어감)", d.get("characters", ""))
        if st.button("💾 이 대본으로 프로젝트 만들기", type="primary"):
            name = datetime.now().strftime("%m%d_%H%M_") + safe_name(title)
            p = config.PROJECTS / name
            (p / "재료").mkdir(parents=True, exist_ok=True)
            data = dict(d, cuts=list(cuts), title=title, tags=tags, description=desc, pinned_comment=pinned,
                        thumbnail=[x.strip() for x in th.split("|")], characters=chars,
                        channel=st.session_state.get("draft_channel", ch["id"]),
                        analysis=st.session_state.get("analysis", ""))
            save_script(p, data)
            st.success(f"저장했어요 → ② 재료 만들기 탭으로 가세요. (폴더: {p.name})")

# ================= ② 재료 만들기 =================
if page == "② 재료 만들기":
    ps = projects()
    if not ps:
        st.info("먼저 ① 탭에서 대본을 저장하세요.")
    else:
        p = st.selectbox("프로젝트", ps, format_func=lambda x: x.name, key="p2")
        with st.expander("🗑 이 프로젝트 취소(지우기)"):
            st.caption("지운 프로젝트는 projects/_지운프로젝트 폴더로 옮겨져요. 실수로 지웠으면 거기서 다시 꺼내면 돼요.")
            if st.checkbox(f"'{p.name}' 프로젝트를 정말 지울게요", key=f"del_ok_{p.name}"):
                if st.button("🗑 프로젝트 지우기", type="primary", key="del_proj"):
                    import shutil
                    trash = config.PROJECTS / "_지운프로젝트"
                    trash.mkdir(exist_ok=True)
                    dest = trash / p.name
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.move(str(p), str(dest))
                    st.success("지웠어요.")
                    st.rerun()
        data = load_script(p)
        fmt = data.get("format", "shorts")
        aspect = "16:9" if fmt == "longform" else "9:16"
        ratio_ko = "가로 16:9" if fmt == "longform" else "세로 9:16"
        chs_by_id = {c["id"]: c for c in channels().values()}
        style = art_style(cfg)[0] + ". " + chs_by_id.get(data.get("channel"), {}).get("visual_style", "vertical 9:16, no text")
        st.markdown("**방법 A — 자동(API, 유료)**: 버튼 하나로 이미지·대사 영상을 만듭니다.  \n"
                    "**방법 B — 플로우(무료)**: 아래 문구를 플로우에 넣어 만든 뒤, 컷마다 파일을 올리세요.")
        # ---- 👤 등장인물 기준 그림: 모든 컷에서 같은 사람 나오게 ----
        ref = p / "재료" / "인물기준.png"
        st.markdown("### 👤 등장인물 기준 그림 (모든 컷 같은 사람)")
        rc1, rc2 = st.columns([3, 2])
        rc1.caption("주인공들이 한 화면에 나란히 선 그림 1장. 자동 그림은 이걸 보고 같은 얼굴로 그리고, "
                    "플로우에서는 이 그림을 '참고 이미지(재료)'로 넣으면 같은 사람이 나와요.")
        if rc1.button("👤 기준 그림 만들기 (1장, 수십 원)"):
            try:
                with st.spinner("기준 그림 만드는 중…"):
                    ai.make_image(cfg, f"Character reference sheet: all main characters standing side by side, left to right in the order listed, full body, "
                                       f"facing camera, plain bright background, clear faces. {data.get('characters','')}. "
                                       "Ordinary Korean people with natural, pleasant faces, looking 5-10 years younger than described, neat and clean. "
                                       "Bright, airy, high-key lighting, no text. ART STYLE (must follow): " + art_style(cfg)[0] + ".",
                                  ref, aspect=aspect)
            except Exception as e:
                st.error(f"오류: {e}")
        rc1.text_area("플로우에서 기준 그림 만들 문구 (무료)",
                      f"{art_style(cfg)[1]}. 등장인물 전원이 왼쪽부터 순서대로 나란히 서 있는 전신 그림, 정면, 밝고 단순한 배경, 실제 나이보다 젊고 깔끔하게, "
                      f"얼굴이 또렷하게, 글자 없이. 등장인물: {data.get('characters','')}", height=100, key="ref_flow")
        upr = rc1.file_uploader("또는 플로우에서 만든 인물 그림 올리기", type=["png", "jpg", "jpeg", "webp"], key="ref_up")
        if new_upload(f"{p.name}_ref", upr):
            ref.write_bytes(upr.getbuffer())
        if ref.exists():
            show_small(rc2, ref, caption="인물 기준 그림")
            rc2.download_button("⬇ 기준 그림 받기 (플로우에 넣기)", ref.read_bytes(), file_name="인물기준.png",
                                mime="image/png", key="dl_ref")
        st.divider()
        colA, colB = st.columns(2)
        mode_video = colA.checkbox("대사 영상도 자동으로 (구글 Omni — 유료, 1컷 수백 원)", value=False)
        make_still = colA.checkbox("대사 컷 '첫 장면 그림'도 만들기 (플로우 시작 이미지용 — 1장 수십~백여 원)", value=False)
        colA.caption("둘 다 끄면: 그림 컷(2·4·6)만 만들어요. 대사 컷은 플로우에서 만들어 올리세요 (가장 저렴)")
        if colB.button("⚡ 빠진 재료 전부 자동으로 만들기", type="primary", width="stretch"):
            log = Log()
            # 그림 컷은 4장씩 동시에 만들기 (한 장씩 차례로 하면 너무 오래 걸림)
            from concurrent.futures import ThreadPoolExecutor, as_completed
            jobs = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for c in data["cuts"]:
                    no = int(c["no"])
                    if c["kind"] != "image":
                        continue
                    shots = [("", c["visual"])] + [(t, v) for t, v in zip(EXTRA, c.get("shots") or [])]
                    for tag, vis in shots:
                        target = p / "재료" / (f"{no:02d}.png" if not tag else f"{no:02d}_{tag}.png")
                        if (not tag and cut_file(p, no)) or (tag and list((p / "재료").glob(f"{no:02d}_{tag}.*"))):
                            continue
                        base = (f"Scene {no}: {vis}. Style: {style}. Characters: {data.get('characters','')}. "
                                "Ordinary everyday Korean people with average, natural faces, light natural makeup, "
                                "tidy look. ONE single scene in one frame — no collage, no panels, no split screen, no comic layout")
                        jobs[pool.submit(ai.make_image, cfg, base, target, aspect, ref)] = f"{no}{'-' + tag if tag else ''}"
                log(f"그림 {len(jobs)}장을 4장씩 동시에 만드는 중…")
                done_n = 0
                for fu in as_completed(jobs):
                    done_n += 1
                    try:
                        fu.result(); log(f"  ✔ 컷 {jobs[fu]} 완료 ({done_n}/{len(jobs)})")
                    except Exception as e:
                        log(f"  ✖ 컷 {jobs[fu]} 실패: {e}")
            for c in data["cuts"]:
                no = int(c["no"])
                if cut_file(p, no) or c["kind"] == "image":
                    continue
                base = (f"Scene {no}: {c['visual']}. Style: {style}. Characters: {data.get('characters','')}. "
                        "Ordinary everyday Korean people with average, natural faces, light natural makeup, "
                        "tidy look. ONE single scene in one frame — no collage, no panels, no split screen, no comic layout")
                try:
                    if c["kind"] == "image":
                        log(f"컷 {no}: 이미지 만드는 중")
                        ai.make_image(cfg, base, p / "재료" / f"{no:02d}.png", ref=ref, aspect=aspect)
                    else:
                        if not (mode_video or make_still):
                            continue
                        still = p / "재료" / f"{no:02d}_첫장면.png"
                        if not still.exists():
                            log(f"컷 {no}: 첫 장면 그림 만드는 중 (플로우 시작 이미지용)")
                            ai.make_image(cfg, base + f". The speaker ({c.get('speaker_en') or c.get('speaker','')}) is in the foreground, "
                                          "face very close to the camera, filling most of the frame, eyes sharp and expressive, about to speak.", still, ref=ref, aspect=aspect)
                        if not mode_video:
                            continue
                        log(f"컷 {no}: 대사 영상 만드는 중 (1~3분)")
                        # 대사·말하는 사람을 맨 앞에 못 박기 (컷마다 대사가 같아지거나 엉뚱한 사람이 말하는 것 방지)
                        vprompt = (f"DIALOGUE — the ONLY words spoken in this clip, in Korean, exactly once, word for word: "
                                   f"\"{c['line']}\". "
                                   f"SPEAKER: {c.get('speaker_en') or c.get('speaker','')}. Only this person moves their lips "
                                   "and speaks; every other person stays completely silent with mouth closed. "
                                   "Do not add any other lines. "
                                   f"Scene {no}: {c['visual']}. Characters: {data.get('characters','')}. {style}. "
                                   "Tight close-up on the speaker's face (face fills most of the frame, eyes sharp). Natural lip sync, no background music, no subtitles, no text on screen.")
                        ai.make_video(dict(cfg, video_seconds=ai.video_seconds_for(c.get("line"))), soften(vprompt), p / "재료" / f"{no:02d}.mp4", first_frame=still, aspect=aspect, log=log)
                except Exception as e:
                    log(f"컷 {no} 실패: {e}")
            log("끝! 아래에서 확인하세요.")
        st.divider()

        # ---- 플로우(무료) 한 번에: 전체 문구 + 한꺼번에 올리기 ----
        short_ = [int(c["no"]) for c in data["cuts"] if c["kind"] == "video" and 0 < len((c.get("line") or "").strip()) < 15]
        if short_:
            st.warning(f"대사 컷 {short_} 의 대사가 15자보다 짧아서 4초 영상에 빈 시간이 남아요.")
            if st.button("✍️ 짧은 대사를 15~20자로 늘리기 (뜻은 그대로, 몇 원)"):
                try:
                    with st.spinner("대사 늘리는 중…"):
                        ai.fix_short_lines(cfg, data)
                    save_script(p, data)
                    st.success("대사를 늘렸어요. 플로우 영상 문구에도 바로 반영돼요.")
                    st.rerun()
                except Exception as e:
                    st.error(f"오류: {e}")
        if data.get("format") == "longform":
            est = ai.long_minutes(data)
            with st.expander(f"📏 같은 이야기로 길이 늘리기 (지금 대본 약 {est:.0f}분)", expanded=est < 8):
                st.caption("줄거리·결말은 그대로, 원래 장면도 글 그대로 두고 사이사이에 새 장면을 끼워 넣어요. "
                           "이미 만든 그림은 번호만 바뀌어 제자리에 그대로 남고, 새 장면 그림만 플로우에서 더 만들면 돼요. "
                           "(유튜브 중간광고는 8분이 넘어야 넣을 수 있어요)")
                goal = st.slider("목표 길이(분)", 9, 40, max(10, min(40, int(est) * 2 + 2)), key="expand_goal")
                if st.button(f"📏 {goal}분으로 늘리기 (Claude 몇백 원, 1~3분)", type="primary"):
                    try:
                        import json as _js, shutil as _sh
                        (p / "script_늘리기전.json").write_text(_js.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        chs = {c_["id"]: c_ for c_ in channels().values()}
                        rules = chs.get(data.get("channel"), {}).get("rules", [])
                        with st.spinner("장면 늘리는 중… (창을 닫지 마세요)"):
                            mapping = ai.expand_long(cfg, data, goal, rules)
                        # 재료 파일 번호 옮기기 (뒤에서부터 → 임시 이름 → 새 이름)
                        tmp = p / "재료" / "_옮기는중"
                        tmp.mkdir(exist_ok=True)
                        moved = []
                        for old, new in mapping:
                            if old == new:
                                continue
                            for f in list((p / "재료").glob(f"{old:02d}.*")) + list((p / "재료").glob(f"{old:02d}_*")):
                                rest = f.name[len(f"{old:02d}"):]
                                _sh.move(str(f), str(tmp / f"{new:02d}{rest}"))
                                moved.append(f)
                        for f in tmp.iterdir():
                            _sh.move(str(f), str(p / "재료" / f.name))
                        tmp.rmdir()
                        _sh.rmtree(p / "work", ignore_errors=True)   # 목소리 파일은 번호가 바뀌어 다시 만듦
                        save_script(p, data)
                        newn = [c["no"] for c in data["cuts"] if c.get("new")]
                        st.success(f"약 {ai.long_minutes(data):.0f}분으로 늘렸어요. 새 장면 {len(newn)}개 — 아래 플로우 문구에서 "
                                   "그림이 없는 장면만 만들어 올리면 돼요. (원래 대본은 script_늘리기전.json 에 보관)")
                        st.rerun()
                    except Exception as e:
                        st.error(f"오류: {e}")
        no_shots = [int(c["no"]) for c in data["cuts"] if c["kind"] == "image" and not c.get("shots")]
        if no_shots:
            st.info(f"그림 컷 {no_shots} 은 화면이 1장뿐이라 오래 멈춰 보여요. 아래 버튼을 누르면 "
                    "대본은 그대로 두고 컷마다 '추가 화면 2개'(나레이션 가운데·끝부분에 맞는 장면)를 써 넣어요.")
            if st.button("🪄 그림 컷마다 추가 화면 2개 만들기 (대본 그대로, 몇 원)", type="primary"):
                try:
                    with st.spinner("추가 화면 쓰는 중…"):
                        shots = ai.add_shots(cfg, data)
                    for c in data["cuts"]:
                        if str(c["no"]) in shots and c["kind"] == "image":
                            c["shots"] = [soften(x) for x in shots[str(c["no"])]][:2]
                    save_script(p, data)
                    st.success("추가 화면을 넣었어요. 위 '그림 전체 문구'에 2-2, 2-3번이 생겼어요.")
                    st.rerun()
                except Exception as e:
                    st.error(f"오류: {e}")
        with st.expander("🗑 여러 컷 재료 한꺼번에 지우기"):
            have = [int(c["no"]) for c in data["cuts"]
                    if cut_file(p, int(c["no"])) or (p / "재료" / f"{int(c['no']):02d}_첫장면.png").exists()]
            vids_ = [int(c["no"]) for c in data["cuts"] if c["kind"] == "video" and int(c["no"]) in have]
            imgs_ = [int(c["no"]) for c in data["cuts"] if c["kind"] == "image" and int(c["no"]) in have]
            q1, q2, q3 = st.columns(3)
            if q1.button(f"대사 컷 모두 고르기 {vids_}"):
                st.session_state["del_pick"] = vids_
            if q2.button(f"그림 컷 모두 고르기 {imgs_}"):
                st.session_state["del_pick"] = imgs_
            if q3.button("고르기 지우기"):
                st.session_state["del_pick"] = []
            if st.session_state.pop("_clear_del", False):
                st.session_state["del_pick"] = []
                st.success("지웠어요. 다시 만들거나 올려 주세요.")
            st.session_state["del_pick"] = [x for x in st.session_state.get("del_pick", []) if x in have]
            pick = st.multiselect("지울 컷 (재료가 있는 컷만 나와요)", have, key="del_pick")
            also_still = st.checkbox("대사 컷의 '첫 장면 그림'도 같이 지우기", value=False)
            if pick and st.button(f"🗑 컷 {pick} 재료 지우기", type="primary"):
                for no in pick:
                    for old_f in list((p / "재료").glob(f"{no:02d}.*")) + list((p / "재료").glob(f"{no:02d}_[bc].*")):
                        old_f.unlink()
                    if also_still:
                        for old_f in (p / "재료").glob(f"{no:02d}_첫장면.*"):
                            old_f.unlink()
                st.session_state["_clear_del"] = True
                st.rerun()
        with st.expander("🔪 여러 칸으로 나온 그림 나누기 (한 장에 장면 2~4개가 위아래로 쌓여 나왔을 때)"):
            st.caption("나눈 그림은 빈 그림 컷에 차례로 들어가요. 가로로 긴 그림은 영상에서 잘리지 않게 "
                       "가운데 크게 놓고 뒤는 흐리게 채워요.")
            sp = st.file_uploader("여러 칸 그림 올리기", type=["png", "jpg", "jpeg", "webp"], key="split_up")
            npan = st.radio("몇 칸인가요?", [2, 3, 4], index=1, horizontal=True, key="split_n")
            if sp is not None and st.button("🔪 나눠서 빈 컷에 넣기"):
                tmp = p / "work"; tmp.mkdir(exist_ok=True)
                src = tmp / ("split_src" + Path(sp.name).suffix.lower()); src.write_bytes(sp.getbuffer())
                pieces = assemble.split_panels(src, int(npan), tmp, "split")
                empties = [int(c["no"]) for c in data["cuts"] if c["kind"] == "image" and not cut_file(p, int(c["no"]))]
                if not empties:
                    empties = [int(c["no"]) for c in data["cuts"] if not cut_file(p, int(c["no"]))]
                put = []
                for pc, no in zip(pieces, empties):
                    (p / "재료" / f"{no:02d}.png").write_bytes(pc.read_bytes()); put.append(no)
                st.success(f"{len(pieces)}장으로 나눠 컷 {put} 에 넣었어요." + ("" if len(put) == len(pieces) else " (빈 컷이 모자라 일부는 안 넣었어요)"))
        st.markdown("### 🆓 플로우로 한 번에 만들기 (무료)")
        common = (f"{ratio_ko}, {art_style(cfg)[1]}, 화면은 밝게(햇살·환한 조명), 배경·건물·옷은 이야기 시대와 장소 그대로(옛날 초가 주막이면 허름한 초가 그대로 — 더 부유하거나 현대식으로 바꾸지 말 것), 정지 그림은 음식 클로즈업이나 인물 얼굴 클로즈업(멀리서 형체만 보이는 장면 금지), 인물은 옛날 이야기라도 단정하고 깔끔하게, 얼굴은 아주 가깝게(얼굴이 화면을 거의 채우고 눈동자까지 또렷하게 보여 감정이 읽히게), 두 사람이면 한 사람 얼굴을 크게 앞에·상대는 뒤에 작고 흐리게, 주변은 흐리고 단순하게(지저분한 물건 없이), 한 장에 한 장면만(여러 칸·분할 화면·콜라주 금지), 글자·자막 없이. 장면마다 볼거리 하나 — 감정이 드러난 큰 얼굴(눈물 고인 눈·놀란 눈·환한 웃음, 무표정 금지) 또는 이야기 속 음식·물건 아주 가까이, 사람 없는 빈 가게·부엌이나 손님 여럿 보이는 가게 전경 금지. 호감 가는 평범한 일반인 얼굴(실제 나이보다 젊어 보이게, 화장 옅게, "
                  f"자연스러운 피부). 모든 장면에서 같은 인물: {data.get('characters','')}")
        lines_all = ["아래 그림들을 순서대로 만들어 줘. 각 그림의 이름(제목)은 줄 맨 앞 [ ] 안의 이름 그대로 지어 줘 (예: c02, c02-2).",
                     "【인물 규칙 — 꼭 지킬 것】 첨부한 '인물 기준 그림'의 사람들만 나온다. 장면마다 같은 얼굴·머리·옷 그대로. "
                     "새로운 주인공을 만들지 말 것. 모든 인물은 한국 사람. 식당 손님·하객 같은 배경 사람은 아주 작고 흐리게(얼굴 안 보이게).",
                     f"등장인물: {data.get('characters','')}",
                     f"공통: {common}", ""]
        vid_lines = ["아래 대사 영상들을 차례로 만들어 줘. 각 영상 이름은 줄 맨 앞 [ ] 안의 이름 그대로(예: v01). 첨부한 인물 기준 그림과 같은 사람으로. "
                     f"{ratio_ko}, 영상 길이는 번호마다 적힌 초, 말하는 사람 얼굴을 아주 가깝게(눈동자까지), 다른 사람은 말하지 않음, 밝게, 자막 없이.", ""]
        only_missing = st.checkbox("그림이 아직 없는 컷만 문구에 넣기 (늘린 새 장면만 만들 때)",
                                   value=any(c.get("new") for c in data["cuts"]), key="flow_only_missing")
        # 새 프로젝트(아직 그림 없음)는 그림 번호를 c01, c02, c03 … 차례대로 매김 (플로우가 번호를 당겨 붙여도 맞게)
        if not data.get("naming") and not any(x for x in (p / "재료").glob("*") if x.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and "기준" not in x.name):
            data["naming"] = "seq"; save_script(p, data)
        seqm = data.get("naming") == "seq"
        if seqm:
            lines_all[0] = "아래 그림들을 순서대로 만들어 줘. 각 그림의 이름(제목)은 줄 맨 앞 [ ] 안의 이름 그대로 지어 줘 (c01, c02, c03 … 차례대로)."
        for c in data["cuts"]:
            if only_missing and cut_file(p, int(c["no"])):
                continue
            if c["kind"] == "video":
                vid_lines.append(f"[v{int(c['no']):02d}] {ai.video_seconds_for(c.get('line'))}초 영상, {c.get('speaker_en') or c.get('speaker','')} 말함: \"{c['line']}\" / 장면: {c['visual']}")
            else:
                tagf = (lambda sl_: f"c{seq_no(data, sl_):02d}") if seqm else (lambda sl_: f"c{int(str(sl_).split('-')[0]):02d}" + (f"-{str(sl_).split('-')[1]}" if "-" in str(sl_) else ""))
                lines_all.append(f"[{tagf(int(c['no']))}] {c['visual']}")
                for k_, v_ in enumerate(c.get("shots") or [], 2):
                    sk_ = f"{int(c['no'])}-{k_}"
                    lines_all.append(f"[{tagf(sk_)}] {v_}")
        lines_all = [soften(x) for x in lines_all]
        st.text_area("① 그림 전체 문구 (그림 번호 c01, c02 … 차례대로) — 복사해서 플로우 채팅창에 한 번에" if seqm else "① 그림 전체 문구 (그림 컷만 — 2, 2-2, 2-3, 4 … 순서) — 복사해서 플로우 채팅창에 한 번에",
                     "\n".join(lines_all), height=220, key="flow_all")
        if len(vid_lines) > 2:
            st.text_area("②-1 대사 영상 문구 한 번에 — ①의 그림이 다 나온 뒤, 같은 채팅창에 이어서 붙여 넣기",
                         "\n".join(soften(x) for x in vid_lines), height=150, key="flow_vids")
        empty = [int(c["no"]) for c in data["cuts"] if not cut_file(p, int(c["no"]))]
        with st.expander("📂 받은 순서대로 넣기 (가장 정확) — 다운로드 폴더에서 '받은 시간 순서'로 바로 가져오기", expanded=True):
            st.caption("플로우에서 그림을 1번부터 차례로 받았다면, 받은 시간 순서 = 컷 순서예요. "
                       "파일 이름이 뒤죽박죽이어도 상관없어요.")
            imp_dir = st.text_input("그림을 받은 폴더 (주소창에서 복사해 붙여 넣기)", cfg.get("import_dir", str(Path.home() / "Downloads")),
                                    key="imp_dir")
            ic1, ic2, ic3 = st.columns(3)
            mins = ic1.number_input("최근 몇 분 안에 받은 것", 5, 1440, 120, step=10)
            rev = ic2.checkbox("거꾸로 (최신 → 오래된 순)", value=False)
            dd2 = ic3.checkbox("비슷한 그림 2장이면 한 장만", value=True)
            folder = Path(imp_dir.strip().strip('"'))
            if folder.is_dir():
                import time as _t
                now_ = _t.time()
                files_ = [f for f in folder.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                          and now_ - f.stat().st_mtime <= mins * 60 and "기준" not in f.name]
                files_.sort(key=lambda f: f.stat().st_mtime, reverse=rev)
                if dd2 and len(files_) > 1:
                    kept = []
                    for f in files_:
                        sg = _img_sig(f.name, f.stat().st_size, f.read_bytes())
                        if not any(_similar(sg, k_[1]) for k_ in kept):
                            kept.append((f, sg))
                    files_ = [k_[0] for k_ in kept]
                slots_ = []
                for c in data["cuts"]:
                    if c["kind"] != "image":
                        continue
                    n_ = int(c["no"])
                    slots_.append(str(n_))
                    slots_ += [f"{n_}-{k_}" for k_ in range(2, 2 + len(c.get("shots") or []))]
                st.write(f"그림 **{len(files_)}장** 찾음 → 그림 자리 **{len(slots_)}곳** (2, 2-2, 2-3, 4 … 순서)")
                if files_:
                    pv = st.columns(8)
                    for i_, (f, sl) in enumerate(list(zip(files_, slots_))[:16]):
                        try:
                            pv[i_ % 8].image(_small(str(f), f.stat().st_mtime, 160), caption=sl, width="stretch")
                        except Exception:
                            pass
                    if len(files_) != len(slots_):
                        st.warning("그림 수와 자리 수가 달라요. 받은 그림이 빠졌거나 더 있는지 확인하세요 (남는 쪽은 안 넣어요).")
                    by_name = {}
                    for f in files_:        # 같은 자리 2장이면 먼저 받은 것만
                        sl_ = slot_of(f.name, data)
                        if sl_ is not None and str(sl_) not in by_name:
                            by_name[str(sl_)] = f
                    if by_name:
                        st.info(f"파일 이름(c02, c02-2 …)으로 {len(by_name)}장의 자리를 찾았어요 → 이름대로 넣어요.")
                    if st.button("📥 그림 자리에 넣기 (이름이 있으면 이름대로, 없으면 받은 순서대로)", type="primary"):
                        pairs_ = list(by_name.items()) if by_name else list(zip(slots_, files_))
                        pairs_ = [(f, sl) for sl, f in pairs_] if by_name else [(f, sl) for sl, f in pairs_]
                        for f, sl in pairs_:
                            vids_ = {int(c["no"]) for c in data["cuts"] if c["kind"] == "video"}
                            if "-" in sl:
                                n_, sub_ = sl.split("-")
                                if int(sub_) not in (2, 3):
                                    continue
                                stem = f"{int(n_):02d}_{EXTRA[int(sub_) - 2]}"
                            elif int(sl) in vids_:
                                stem = f"{int(sl):02d}_첫장면"      # 대사 영상 컷의 그림 = 첫 장면 그림
                            else:
                                stem = f"{int(sl):02d}"
                            for old in (p / "재료").glob(f"{stem}.*"):
                                old.unlink()
                            (p / "재료" / f"{stem}{'.png' if stem.endswith('_첫장면') else f.suffix.lower()}").write_bytes(f.read_bytes())
                        cfg["import_dir"] = str(folder); config.save(cfg)
                        st.success(f"{min(len(files_), len(slots_))}장을 받은 순서대로 넣었어요.")
                        st.rerun()
            else:
                st.caption("폴더를 찾을 수 없어요. 파일 탐색기 주소창을 눌러 주소를 복사해 붙여 넣으세요.")
        ups = st.file_uploader("② 플로우에서 받은 파일들을 한꺼번에 올리기 — 올린 뒤 그림을 보고 컷 번호만 골라 주세요",
                               type=["png", "jpg", "jpeg", "webp", "mp4", "mov"],
                               accept_multiple_files=True, key="bulk_up")
        dedup = st.checkbox("같은 장면이 2장씩 나왔으면 한 장만 쓰기 (비슷한 그림은 자동으로 빼기)", value=True, key="dedup")
        if ups:
            def _save(u, no):
                if isinstance(no, str) and no.endswith("-시작"):   # 대사 컷의 시작 그림
                    stem = f"{int(no.split('-')[0]):02d}_첫장면"
                elif isinstance(no, str) and "-" in no:      # 그림 컷의 2·3번째 화면
                    n_, sub_ = no.split("-")
                    stem = f"{int(n_):02d}_{EXTRA[int(sub_) - 2]}"
                else:
                    stem = f"{int(no):02d}"
                for old in (p / "재료").glob(f"{stem}.*"):
                    old.unlink()
                ext = ".png" if stem.endswith("_첫장면") else Path(u.name).suffix.lower()
                (p / "재료" / f"{stem}{ext}").write_bytes(u.getbuffer())

            sig = p.name + "|" + "|".join(f"{u.name}:{u.size}" for u in ups)
            if st.session_state.get("_bulk_sig") != sig:
                # 새로 올린 묶음 → 빈 자리에 순서대로 '바로' 넣기 (그림: 2 → 2-2 → 2-3 → 4 …, 영상: 1 → 3 …)
                vid_slots = [int(c["no"]) for c in data["cuts"] if c["kind"] == "video" and not cut_file(p, int(c["no"]))]
                img_slots = []
                for c in data["cuts"]:
                    n_ = int(c["no"])
                    if c["kind"] == "video":
                        continue      # 대사 영상 자리(1·3·5…)에는 그림을 자동으로 넣지 않음
                    if not cut_file(p, n_):
                        img_slots.append(n_)
                    have_x = [x.stem.split("_")[1] for x in extra_files(p, n_)]
                    for k_, t_ in list(enumerate(EXTRA, 2))[:len(c.get("shots") or [])]:   # 대본에 추가 화면이 있을 때만
                        if t_ not in have_x:
                            img_slots.append(f"{n_}-{k_}")
                plan0, vi, ii = [], 0, 0
                for u in ups:
                    if Path(u.name).suffix.lower() in (".mp4", ".mov"):
                        plan0.append(vid_slots[vi] if vi < len(vid_slots) else "넣지 않음"); vi += 1
                    else:
                        plan0.append(img_slots[ii] if ii < len(img_slots) else "넣지 않음"); ii += 1
                # ① 파일 이름에 자리(c02, c02-2, v01)가 있으면 그대로 — 가장 정확
                named, dup_ = 0, 0
                vid_nos_ = {int(c["no"]) for c in data["cuts"] if c["kind"] == "video"}
                seen_ = set()
                for k, u in enumerate(ups):
                    sl = slot_of(u.name, data)
                    if sl is None:
                        continue
                    is_img_ = Path(u.name).suffix.lower() not in (".mp4", ".mov")
                    if is_img_ and isinstance(sl, int) and sl in vid_nos_:
                        sl = f"{sl}-시작"          # 대사 영상 컷의 그림 = 영상 만들 첫 장면 그림
                    if (sl, is_img_) in seen_:      # 같은 자리 그림이 2장(플로우가 2장씩 만든 것) → 먼저 받은 것만
                        plan0[k] = "넣지 않음"; dup_ += 1
                        continue
                    seen_.add((sl, is_img_)); plan0[k] = sl; named += 1
                if named:
                    st.info(f"파일 이름으로 {named}개 자리를 찾았어요." + (f" 같은 자리에 2장 이상인 {dup_}장은 빼 두었어요(아래 목록에서 바꿀 수 있어요)." if dup_ else ""))
                # ② 이름이 없는 그림만 AI가 보고 맞추기
                pic_idx = [k for k, u in enumerate(ups) if Path(u.name).suffix.lower() not in (".mp4", ".mov")
                           and slot_of(u.name, data) is None]
                if dedup and len(pic_idx) > 1:
                    # 비슷한 그림(같은 문구로 2장 나온 것) 찾기 → 한 장만 남김
                    keep, dropped = [], 0
                    sigs = {k: _img_sig(ups[k].name, ups[k].size, ups[k].getvalue()) for k in pic_idx}
                    for k in pic_idx:
                        if any(_similar(sigs[k], sigs[j]) for j in keep):
                            plan0[k] = "넣지 않음"; dropped += 1
                        else:
                            keep.append(k)
                    pic_idx = keep
                    if dropped:
                        st.info(f"비슷한 그림 {dropped}장을 빼고 {len(keep)}장으로 자리를 맞췄어요. (빠진 그림은 아래 목록에서 '넣지 않음'으로 보여요)")
                if pic_idx and img_slots and cfg.get("anthropic_api_key"):
                    desc = {}
                    for c in data["cuts"]:
                        if c["kind"] != "image":
                            continue
                        n_ = int(c["no"])
                        allv = [c.get("visual", "")] + list(c.get("shots") or [])
                        for k_, v_ in enumerate(allv, 1):
                            key_ = str(n_) if k_ == 1 else f"{n_}-{k_}"
                            if (n_ if k_ == 1 else key_) in img_slots:
                                desc[key_] = v_
                    try:
                        # 20장씩 나눠서 맞추기 (한 번에 너무 많으면 AI가 못 받음) — 이미 찬 자리는 다음 묶음에서 뺌
                        remaining = dict(desc)
                        bar = st.progress(0.0, text="AI가 그림을 보고 맞는 자리를 찾는 중…")
                        fails = 0
                        for b0 in range(0, len(pic_idx), 12):
                            chunk = pic_idx[b0:b0 + 12]
                            if not remaining:
                                for k in chunk:
                                    plan0[k] = "넣지 않음"
                                continue
                            try:
                                got = ai.match_images(cfg, remaining, [ups[k].getvalue() for k in chunk])
                            except Exception:
                                fails += 1
                                continue      # 이 묶음만 순서대로 둠
                            for k, g in zip(chunk, got):
                                plan0[k] = int(g) if g.isdigit() else g
                                remaining.pop(g, None)
                            bar.progress(min(1.0, (b0 + len(chunk)) / len(pic_idx)),
                                         text=f"AI 자리 맞추기 {min(b0 + len(chunk), len(pic_idx))}/{len(pic_idx)}")
                        if fails:
                            st.warning(f"AI 자리 맞추기 중 {fails}묶음은 못 해서 그 부분만 올린 순서대로 넣었어요. 아래 '🤖 다시 맞추기'를 눌러 보세요.")
                    except Exception as e:
                        st.warning(f"AI 자리 맞추기를 못 해서 올린 순서대로 넣었어요 ({str(e)[:80]}). 아래 '🤖 다시 맞추기'를 눌러 보세요.")
                for u, no in zip(ups, plan0):
                    if no != "넣지 않음":
                        _save(u, no)
                st.session_state["_bulk_sig"] = sig
                st.session_state["_bulk_plan"] = plan0
                st.success(f"{sum(1 for x in plan0 if x != '넣지 않음')}개 파일을 빈 자리에 바로 넣었어요. "
                           "순서가 틀린 게 있으면 아래에서 번호를 바꾸고 '다시 넣기'를 누르세요.")
            plan0 = st.session_state.get("_bulk_plan", [])
            all_nos = ["넣지 않음"]
            for c in data["cuts"]:
                all_nos.append(int(c["no"]))
                if c["kind"] == "image":
                    all_nos += [f"{int(c['no'])}-2", f"{int(c['no'])}-3"]
                else:
                    all_nos.append(f"{int(c['no'])}-시작")
            plan = {}
            # ---- 👆 눌러서 넣기: 자리 설명을 보면서 맞는 그림을 차례로 누르기 (가장 확실) ----
            with st.expander("👆 눌러서 넣기 — 위에 나온 '지금 자리' 설명을 보고 맞는 그림을 누르면 차례로 들어가요", expanded=False):
                order_ = []
                for c in data["cuts"]:
                    if c["kind"] != "image":
                        continue
                    n_ = int(c["no"])
                    allv = [c.get("visual", "")] + list(c.get("shots") or [])
                    for k_, v_ in enumerate(allv, 1):
                        order_.append((str(n_) if k_ == 1 else f"{n_}-{k_}", v_, c.get("narration", "")))
                pos = st.session_state.get("_click_pos", 0)
                used_ = set(st.session_state.get("_click_used", []))
                if pos >= len(order_):
                    st.success("모든 그림 자리를 채웠어요! 🎉")
                    if st.button("처음부터 다시", key="click_reset"):
                        st.session_state["_click_pos"] = 0; st.session_state["_click_used"] = []; st.rerun()
                else:
                    sl, vis, nar = order_[pos]
                    _sn = seq_no(data, sl) if data.get("naming") == "seq" else None
                    st.markdown(f"### 지금 자리: **{('c%02d · 컷 ' % _sn) if _sn else ''}{sl}** ({pos + 1}/{len(order_)})")
                    st.caption(f"나레이션: {nar[:120]}…")
                    st.caption(f"장면: {vis[:200]}")
                    b1, b2 = st.columns(2)
                    if b1.button("⏭ 이 자리 건너뛰기", key="click_skip"):
                        st.session_state["_click_pos"] = pos + 1; st.rerun()
                    if b2.button("⏮ 한 칸 뒤로", key="click_back", disabled=pos == 0):
                        st.session_state["_click_pos"] = max(0, pos - 1); st.rerun()
                    gc = st.columns(8)
                    shown = 0
                    for k, u in enumerate(ups):
                        if Path(u.name).suffix.lower() in (".mp4", ".mov") or u.name in used_:
                            continue
                        box = gc[shown % 8]; shown += 1
                        try:
                            box.image(_upload_thumb(u.name, u.size, u.getvalue()), width="stretch")
                        except Exception:
                            box.write(u.name[:12])
                        if box.button("여기 넣기", key=f"click_{k}_{u.name}"):
                            _save(u, int(sl) if sl.isdigit() else sl)
                            st.session_state["_click_used"] = list(used_ | {u.name})
                            st.session_state["_click_pos"] = pos + 1
                            st.rerun()
            vids_set_ = {int(c["no"]) for c in data["cuts"] if c["kind"] == "video"}
            def slot_label(x):
                if x == "넣지 않음":
                    return "넣지 않음"
                if isinstance(x, str) and x.endswith("-시작"):
                    return f"v{int(x.split('-')[0]):02d} 첫 장면 그림"
                if isinstance(x, int) and x in vids_set_:
                    return f"v{x:02d} 대사 영상"
                n_ = seq_no(data, x) if data.get("naming") == "seq" else None
                return f"c{n_:02d}  (컷 {x})" if n_ else f"컷 {x}"
            def _ord(k):      # 그림 번호 순서대로 보여 주기
                x = plan0[k] if k < len(plan0) else "넣지 않음"
                if x == "넣지 않음":
                    return (2, 0)
                n_ = seq_no(data, x)
                return (0, n_) if n_ else (1, int(str(x).split("-")[0]))
            grid = st.expander(f"올린 파일 {len(ups)}개와 들어간 자리 보기·바꾸기 (그림 번호 순서)", expanded=True)
            cols = grid.columns(8)
            for i_, k in enumerate(sorted(range(len(ups)), key=_ord)):
                u = ups[k]
                box = cols[i_ % len(cols)]
                if Path(u.name).suffix.lower() in (".mp4", ".mov"):
                    box.markdown(f"🎬 **영상**  \n{u.name[:18]}")
                else:
                    try:
                        box.image(_upload_thumb(u.name, u.size, u.getvalue()), width="stretch")
                    except Exception:
                        box.write(u.name[:18])
                g = plan0[k] if k < len(plan0) and plan0[k] in all_nos else "넣지 않음"
                plan[k] = box.selectbox("컷", all_nos, index=all_nos.index(g), key=f"assign_{k}_{u.name}",
                                        label_visibility="collapsed", format_func=slot_label)
            if st.button("🤖 AI로 처음부터 다시 맞추기 (이번에 올린 파일이 들어간 자리를 비우고 다시)"):
                for no in st.session_state.get("_bulk_plan", []):
                    if no == "넣지 않음":
                        continue
                    if isinstance(no, str) and no.endswith("-시작"):
                        stem = f"{int(no.split('-')[0]):02d}_첫장면"
                    elif isinstance(no, str) and "-" in no:
                        n_, sub_ = no.split("-"); stem = f"{int(n_):02d}_{EXTRA[int(sub_) - 2]}"
                    else:
                        stem = f"{int(no):02d}"
                    for old in (p / "재료").glob(f"{stem}.*"):
                        old.unlink()
                st.session_state["_bulk_sig"] = None
                st.rerun()
            if st.button("📥 바꾼 번호로 다시 넣기"):
                for k, u in enumerate(ups):
                    if plan[k] != "넣지 않음":
                        _save(u, plan[k])
                st.session_state["_bulk_plan"] = [plan[k] for k in range(len(ups))]
                st.success("바꾼 번호로 다시 넣었어요.")
        st.divider()
        for c in data["cuts"]:
            no = int(c["no"])
            f = cut_file(p, no)
            l, m, r = st.columns([2, 3, 2])
            l.markdown(f"### 컷 {no} · {'🎥 대사 영상' if c['kind']=='video' else '🖼️ 이미지+나레이션'}")
            if c["kind"] == "image" and data.get("naming") == "seq":
                nums_ = [seq_no(data, no)] + [seq_no(data, f"{no}-{k_}") for k_ in range(2, 2 + len(c.get("shots") or []))]
                l.markdown("**🔢 그림 번호:** " + " · ".join(f"`c{n_:02d}`" for n_ in nums_ if n_))
            elif c["kind"] == "video":
                l.markdown(f"**🔢 영상 번호:** `v{no:02d}`")
            if c.get("chapter"):
                l.markdown(f"**📖 {c['chapter']}**")
            l.write(c.get("line") or c.get("narration", ""))
            if c["kind"] == "video":
                # 플로우 영상은 짧게 — 시작 그림이 인물·배경을 이미 담고 있음 (긴 문구는 거절되기 쉬움)
                flow = (f"첨부한 시작 그림에서 이어지는 {ratio_ko} {ai.video_seconds_for(c.get('line'))}초 영상. {c['visual']}\n"
                        f"{c.get('speaker','')} 말함: \"{c['line']}\"\n"
                        "다른 사람은 말하지 않음. 말하는 사람 얼굴을 아주 가깝게(얼굴이 화면을 거의 채우고 눈동자까지 또렷), 상대는 뒤에 흐리게. "
                        f"시작 그림과 같은 그림체({art_style(cfg)[1].split('(')[0]}), 밝고 환한 분위기, 자막 없이.")
            else:
                _t = (lambda sl_: f"c{seq_no(data, sl_):02d}") if data.get("naming") == "seq" else (lambda sl_: f"컷 {sl_} 장면")
                flow = (f"[{_t(no)}] {c['visual']}"
                        + "".join(f"\n[{_t(f'{no}-{k_}')}] {v_}" for k_, v_ in enumerate(c.get("shots") or [], 2))
                        + f"\n[공통] {ratio_ko}, {art_style(cfg)[1]}, 화면은 밝게(햇살·환한 조명), 배경·건물·옷은 이야기 시대와 장소 그대로 "
                        "(더 부유하거나 현대식으로 바꾸지 말 것), 정지 그림은 음식 클로즈업이나 인물 얼굴 클로즈업(멀리서 형체만 보이는 장면 금지), 인물은 옛날 이야기라도 단정하고 깔끔하게, 얼굴은 아주 가깝게(얼굴이 화면을 거의 채우고 눈동자까지 또렷하게 보여 감정이 읽히게), 두 사람이면 한 사람 얼굴을 크게 앞에·상대는 뒤에 작고 흐리게, 주변은 흐리고 단순하게(지저분한 물건 없이), 한 장에 한 장면만(여러 칸·분할 화면·콜라주 금지), 글자·자막 없이. 평범한 일반인 얼굴(화장 옅게, 자연스러운 피부). "
                        + f"등장인물: {data.get('characters','')}")
            flow = soften(flow)
            m.text_area("플로우에 넣을 문구 (복사) — 맨 위가 이 컷만의 장면", flow, height=170, key=f"flow{no}")
            up = m.file_uploader("플로우에서 받은 파일 올리기", type=["png", "jpg", "jpeg", "webp", "mp4", "mov"], key=f"up{no}")
            if new_upload(f"{p.name}_{no}", up):
                for old in (p / "재료").glob(f"{no:02d}.*"):
                    old.unlink()
                ext = Path(up.name).suffix.lower()
                (p / "재료" / f"{no:02d}{ext}").write_bytes(up.getbuffer())
                f = cut_file(p, no)
            if f:
                if f.suffix.lower() in (".mp4", ".mov"):
                    r.success(f"🎬 영상 있음 ({f.name})")
                    if r.checkbox("▶ 재생해 보기", key=f"play{no}"):
                        r.video(str(f))
                else:
                    show_small(r, f)
            if c["kind"] == "image":
                ex = extra_files(p, no)
                r.caption(f"추가 화면 {len(ex)}/2장 (있으면 5~6초마다 번갈아 보여 줘요)")
                for e_ in ex:
                    show_small(r, e_)
                    if r.button(f"🗑 {e_.stem} 지우기", key=f"delx_{e_.stem}"):
                        e_.unlink(); st.rerun()
                if r.button(f"🗑 컷 {no} 재료 지우기", key=f"del{no}"):
                    f.unlink(); st.session_state.pop(f"_up_{p.name}_{no}", None); st.rerun()
            else:
                still = p / "재료" / f"{no:02d}_첫장면.png"
                if c["kind"] == "video" and still.exists():
                    show_small(r, still, caption="첫 장면 그림 (플로우 시작 이미지)")
                    r.download_button("⬇ 첫 장면 그림 받기", still.read_bytes(), file_name=f"컷{no}_첫장면.png",
                                      mime="image/png", key=f"dl{no}")
                    r.warning("대사 영상을 플로우에서 만들어 올려 주세요")
                    if r.button(f"🗑 컷 {no} 첫 장면 그림 지우기", key=f"delst{no}"):
                        still.unlink(); st.rerun()
                else:
                    r.warning("아직 재료 없음")
        if st.button("📂 재료 폴더 열기"):
            open_folder(p / "재료")

# ================= ③ 완성 영상 =================
if page == "③ 완성 영상":
    ps = projects()
    if not ps:
        st.info("먼저 ① 탭에서 대본을 저장하세요.")
    else:
        p = st.selectbox("프로젝트", ps, format_func=lambda x: x.name, key="p3")
        data = load_script(p)
        fmt = data.get("format", "shorts")
        aspect = "16:9" if fmt == "longform" else "9:16"
        ratio_ko = "가로 16:9" if fmt == "longform" else "세로 9:16"
        missing = [int(c["no"]) for c in data["cuts"] if not cut_file(p, int(c["no"]))]
        if missing:
            st.warning(f"재료가 없는 컷: {missing} — ② 탭에서 채워 주세요.")
        with st.expander("🎨 썸네일 전용 그림 (영상 장면 말고, 주인공 중심·밝게)", expanded=True):
            sp_ = p / "재료" / "썸네일전용.png"
            u1, u2 = st.columns([3, 2])
            u1.caption("영상 장면을 쓰지 않고 썸네일만을 위한 그림을 따로 만들어요. "
                       "인물 기준 그림이 있으면 같은 얼굴로 그려요.")
            if u1.button("🎨 썸네일 그림 만들기 (1장, 수십 원)", key="mk_thimg"):
                try:
                    with st.spinner("썸네일 그림 만드는 중…"):
                        make_thumb_image(p, data, fmt); make_thumbs(p, data, fmt)
                    st.success("썸네일 그림과 썸네일 A·B를 새로 만들었어요.")
                except Exception as e:
                    st.error(f"오류: {e}")
            u1.text_area("또는 플로우에 넣을 문구 (무료)",
                         "썸네일용 사진. 주인공 가슴 위까지 가깝게, 얼굴을 아주 크게(이마부터 턱까지 화면 높이의 절반, 눈동자까지 또렷하게), 얼굴은 가로 가운데(가장자리에 잘리지 않게), 다른 인물은 뒤에 작고 흐리게, " +
                         ("얼굴은 화면 위쪽, 아래쪽은 단순하게 비워 두기. " if fmt != "longform" else "얼굴은 오른쪽, 왼쪽과 아래는 단순하게. ") +
                         "놀람·눈물·분노처럼 감정이 크게 드러나는 표정, 아주 밝은 조명, 배경은 밝고 흐리게, 글자 없이. "
                         f"{ratio_ko}. 등장인물: {data.get('characters','')}", height=110, key="th_flow")
            upt = u1.file_uploader("플로우에서 만든 썸네일 그림 올리기", type=["png", "jpg", "jpeg", "webp"], key="th_up")
            if new_upload(f"{p.name}_thimg", upt):
                for old_ in (p / "재료").glob("썸네일전용.*"):
                    old_.unlink()
                (p / "재료" / "썸네일전용.png").write_bytes(upt.getbuffer())
                make_thumbs(p, data, fmt)
                st.success("올린 그림으로 썸네일 A·B를 만들었어요.")
            if sp_.exists():
                show_small(u2, sp_, caption="썸네일 전용 그림")
                if u2.button("🗑 이 그림 빼기 (장면 그림으로 돌아가기)", key="del_thimg"):
                    sp_.unlink(); make_thumbs(p, data, fmt); st.rerun()
        with st.expander("🖼 썸네일 문구·장면 바꾸기 (영상은 그대로, 썸네일만 다시 만들어요 — 0원)", expanded=True):
            tl = (list(data.get("thumbnail", [])) + ["", ""])[:2]
            t1 = st.text_input("윗줄 (흰 글씨, 10자 안팎)", tl[0], key=f"th1_{p.name}")
            t2 = st.text_input("아랫줄 (노란 큰 글씨, 8자 안팎)", tl[1], key=f"th2_{p.name}")
            pics_ = scene_pics(p)
            names_ = [x.name for x in pics_]
            cur_ = [Path(x).name for x in data.get("thumb_scenes", [])]
            ca, cb = st.columns(2)
            ia = ca.selectbox("썸네일 A 장면", names_, index=names_.index(cur_[0]) if cur_ and cur_[0] in names_ else 0,
                              key="tha") if names_ else None
            ib = cb.selectbox("썸네일 B 장면", names_, index=names_.index(cur_[1]) if len(cur_) > 1 and cur_[1] in names_ else min(1, len(names_) - 1),
                              key="thb") if names_ else None
            if ia:
                show_small(ca, p / "재료" / ia)
            if ib:
                show_small(cb, p / "재료" / ib)
            if st.button("🖼 이 문구·장면으로 썸네일 다시 만들기", key="remake_th"):
                data["thumbnail"] = [t1.strip(), t2.strip(), ""]
                save_script(p, data)
                try:
                    with st.spinner("썸네일 만드는 중…"):
                        make_thumbs(p, data, fmt, picks=[p / "재료" / ia, p / "재료" / ib] if ia and ib else None)
                    st.success("썸네일 A·B를 새로 만들었어요. 아래에서 확인하세요.")
                except Exception as e:
                    st.error(f"오류: {e}")
        with st.expander("✏️ 나레이션·자막 고치기 (고친 뒤 '영상 만들기'를 다시 누르면 반영돼요)"):
            st.caption("그림 컷: 나레이션 글을 고치면 목소리를 새로 만들어요(수십 원).  "
                       "대사 컷: 화면 자막만 바뀌어요 — 영상 속 실제 말소리는 영상을 다시 만들어야 바뀝니다.")
            edits = {}
            for c in data["cuts"]:
                no = int(c["no"])
                if c["kind"] == "image":
                    edits[no] = st.text_area(f"컷 {no} 나레이션", c.get("narration", ""), height=100, key=f"nar_{p.name}_{no}")
                else:
                    edits[no] = st.text_input(f"컷 {no} 대사 자막", c.get("line", ""), key=f"line_{p.name}_{no}")
            if st.button("💾 고친 글 저장", key="save_text"):
                changed = []
                for c in data["cuts"]:
                    no = int(c["no"])
                    field = "narration" if c["kind"] == "image" else "line"
                    if edits[no] != c.get(field, ""):
                        c[field] = edits[no]
                        changed.append(no)
                        if field == "narration":   # 옛 목소리 지우기 → 다음에 새로 만듦
                            for old in (p / "work").glob(f"*_{no:02d}.wav"):
                                old.unlink()
                save_script(p, data)
                st.success(f"저장했어요 (바뀐 컷: {changed or '없음'}). 아래 '🎬 영상 만들기'를 다시 누르세요.")
        mup = st.file_uploader("🎵 배경음악 파일 올리기 (mp3·wav·m4a — 한 번 올리면 music 폴더에 계속 남아요)",
                               type=["mp3", "wav", "m4a"], key="music_up")
        if mup is not None:
            dest = config.MUSIC / mup.name
            if not dest.exists():
                dest.write_bytes(mup.getbuffer())
                st.success(f"음악을 넣었어요: {mup.name} — 아래 목록에서 고르세요.")
        musics = [None] + sorted([m for m in config.MUSIC.iterdir() if m.suffix.lower() in (".mp3", ".wav", ".m4a")])
        music = st.selectbox("배경음악 (music 폴더의 곡)", musics, format_func=lambda m: "없음" if m is None else m.name)
        own_voice = st.checkbox("나레이션을 내가 녹음/다른 곳에서 만든 파일로 쓰기 (재료 폴더에 02_나레이션.mp3 처럼)")
        auto_thumb = st.checkbox("썸네일 전용 그림을 구글로 자동으로 만들기 (약 100원 — 끄면 플로우에서 만든 그림이나 장면 그림으로 무료)",
                                 value=False)
        n_voice = sum(1 for c in data["cuts"] if c["kind"] == "image" and c.get("narration")
                      and not (p / "work" / f"v_{cfg.get('tts_voice','Kore')}_{int(c['no']):02d}.wav").exists())
        st.caption(f"💰 이번에 드는 비용: 나레이션 목소리 {n_voice}개 (약 {n_voice * 5}~{n_voice * 15}원)"
                   + (" + 썸네일 그림 약 100원" if auto_thumb and not (p / "재료" / "썸네일전용.png").exists() else "")
                   + " — 이미 만든 목소리는 다시 만들지 않아요. 영상 편집 자체는 0원.")
        if st.button("🎬 영상 만들기", type="primary", disabled=bool(missing), width="stretch"):
            log = Log()
            try:
                # 나레이션 목소리를 4개씩 동시에 미리 만들기
                from concurrent.futures import ThreadPoolExecutor, as_completed
                (p / "work").mkdir(exist_ok=True)
                tts_fail = []
                need = [c for c in data["cuts"] if c["kind"] == "image" and c.get("narration")
                        and not (own_voice and list((p / "재료").glob(f"{int(c['no']):02d}_나레이션.*")))
                        and not (p / "work" / f"v_{cfg.get('tts_voice','Kore')}_{int(c['no']):02d}.wav").exists()]
                if need:
                    log(f"나레이션 목소리 {len(need)}개를 하나씩 만드는 중… (한 개에 보통 10~40초, 길어도 2분 반이면 넘어가요)")
                    import concurrent.futures as _cf
                    pool = ThreadPoolExecutor(max_workers=len(need))
                    for k, c in enumerate(need, 1):
                        wav = p / "work" / f"v_{cfg.get('tts_voice','Kore')}_{int(c['no']):02d}.wav"
                        fu = pool.submit(ai.make_tts, cfg, c["narration"], wav)
                        try:
                            fu.result(timeout=150)
                            log(f"  ✔ 목소리 {c['no']} ({k}/{len(need)})")
                        except _cf.TimeoutError:
                            tts_fail.append(c["no"])
                            log(f"  ⚠ 컷 {c['no']} 목소리: 구글이 2분 반 넘게 대답이 없어 건너뛰어요 — 자막만 넣고 계속해요")
                        except Exception as e_:
                            tts_fail.append(c["no"])
                            log(f"  ⚠ 컷 {c['no']} 목소리를 못 만들었어요 — 자막만 넣고 계속해요 ({str(e_)[:200]})")
                    pool.shutdown(wait=False, cancel_futures=True)
                cuts = []
                for c in data["cuts"]:
                    no = int(c["no"])
                    media = cut_file(p, no)
                    audio = None
                    if c["kind"] == "image" and c.get("narration"):
                        mine = list((p / "재료").glob(f"{no:02d}_나레이션.*"))
                        if own_voice and mine:
                            wav = p / "work" / f"tts_{no:02d}.wav"
                            (p / "work").mkdir(exist_ok=True)
                            assemble.run(["-i", str(mine[0]), "-ar", "24000", "-ac", "1", str(wav)])
                            audio = wav
                        else:
                            wav = p / "work" / f"v_{cfg.get('tts_voice','Kore')}_{no:02d}.wav"
                            (p / "work").mkdir(exist_ok=True)
                            audio = wav if wav.exists() else None
                    cuts.append(dict(media=media, audio=audio, text=c.get("line") or c.get("narration", ""),
                                     extras=[str(x) for x in extra_files(p, no)] if c["kind"] == "image" else []))
                assemble.set_format(fmt)
                if any(c["kind"] == "video" for c in data["cuts"]):
                    assemble.FPS = 30      # 대사 영상이 있으면 부드럽게 30프레임
                cfg2 = dict(cfg)
                if fmt == "longform":   # 가로 영상: 자막은 아래쪽, 한 줄 길게
                    cfg2.update(subtitle_y=0.86, subtitle_size=58, subtitle_chars=24)
                durs = []
                final = assemble.assemble(p, cuts, cfg2, music=music, log=log, durs=durs)
                chapters_txt = ""
                if fmt == "longform":
                    t_, rows = 0.0, []
                    for c, d_ in zip(data["cuts"], durs):
                        if c.get("chapter"):
                            rows.append(f"{int(t_ // 60)}:{int(t_ % 60):02d} {c['chapter']}")
                        t_ += d_
                    if rows:
                        rows[0] = "0:00 " + rows[0].split(" ", 1)[1]
                        chapters_txt = "\n\n[챕터 — 설명란에 그대로 붙이세요]\n" + "\n".join(rows)
                if auto_thumb and not (p / "재료" / "썸네일전용.png").exists():
                    try:
                        log("썸네일 전용 그림(주인공 얼굴 크게) 만드는 중…")
                        make_thumb_image(p, data, fmt)
                    except Exception as e:
                        log(f"  (썸네일 전용 그림은 건너뜀 — 장면 그림으로 만들어요: {str(e)[:80]})")
                make_thumbs(p, data, fmt)
                info = (f"[제목]\n{data.get('title','')}\n\n[설명]\n{data.get('description','')}{chapters_txt}\n\n"
                        f"[태그]\n{data.get('tags','')}\n\n[고정 댓글]\n{data.get('pinned_comment','')}\n\n"
                        "※ 업로드할 때 '변경되거나 합성된 콘텐츠' 질문에 '예'를 체크하세요.\n")
                (p / "업로드정보.txt").write_text(info, encoding="utf-8")
                st.success("완성! 아래에서 확인하세요.")
            except Exception as e:
                st.error(f"오류: {e}")
        fin = p / "완성본.mp4"
        if fin.exists():
            a, b = st.columns([2, 3])
            a.video(str(fin))
            for nm, cap in (("썸네일_A.jpg", "썸네일 A — 글씨 아래"), ("썸네일_B.jpg", "썸네일 B — 다른 장면/글씨판")):
                if (p / nm).exists():
                    show_small(a, p / nm, caption=cap)
            if (p / "썸네일_A.jpg").exists():
                a.caption("두 개 다 올려 두고, 유튜브 '테스트 및 비교'로 더 잘 되는 쪽을 고르세요.")
            if (p / "업로드정보.txt").exists():
                b.text_area("업로드 정보 (복사해서 쓰세요)", (p / "업로드정보.txt").read_text(encoding="utf-8"), height=420)
            if b.button("📂 완성 폴더 열기"):
                open_folder(p)

# ================= ⚙️ 설정 =================
if page == "⚙️ 설정":
    st.markdown("키는 이 컴퓨터의 settings.json 에만 저장되고, 인터넷(GitHub)에는 올라가지 않습니다.")
    new = dict(cfg)
    new["google_api_key"] = st.text_input("구글 AI Studio 키", cfg["google_api_key"], type="password")
    new["anthropic_api_key"] = st.text_input("Anthropic(클로드) 키", cfg["anthropic_api_key"], type="password")
    with st.expander("모델 이름 (보통은 그대로 두세요)"):
        for k in ["claude_model", "gemini_text_model", "gemini_image_model", "gemini_tts_model", "veo_model"]:
            new[k] = st.text_input(k, cfg[k])
        new["video_seconds"] = st.selectbox("대사 영상 길이(초)", [4, 6, 8], index=[4, 6, 8].index(int(cfg["video_seconds"])))
    names_ = list(ART_STYLES)
    new["art_style"] = st.radio("🎨 그림 느낌", names_, horizontal=True,
                                index=names_.index(cfg.get("art_style", "반실사 일러스트")) if cfg.get("art_style", "반실사 일러스트") in names_ else 0)
    new["brighten"] = st.slider("☀️ 화면 밝게 (완성 영상에서 그림·영상을 환하게 보정, 0 = 보정 안 함)", 0, 10,
                                int(cfg.get("brighten", 2)))
    new["image_size"] = st.radio("그림 해상도 (자동 그림)", ["1K", "2K"], horizontal=True,
                                 index=["1K", "2K"].index(cfg.get("image_size", "2K")) if cfg.get("image_size", "2K") in ("1K", "2K") else 1,
                                 help="1K = 비용 절약(롱폼 추천), 2K = 더 선명(쇼츠 썸네일용)")
    with st.expander("🔊 나레이션 목소리 고르기 (들어 보기)", expanded=True):
        VOICES = {
            "Kore": "여성 · 단단하고 또렷함", "Aoede": "여성 · 산뜻하고 가벼움", "Leda": "여성 · 젊고 밝음",
            "Zephyr": "여성 · 밝고 경쾌함", "Sulafat": "여성 · 따뜻함", "Despina": "여성 · 부드러움",
            "Gacrux": "여성 · 차분하고 성숙함", "Vindemiatrix": "여성 · 다정하고 온화함", "Achernar": "여성 · 부드럽고 차분함",
            "Charon": "남성 · 설명하듯 차분함", "Puck": "남성 · 밝고 경쾌함", "Orus": "남성 · 단단함",
            "Fenrir": "남성 · 흥분된·생동감", "Achird": "남성 · 친근함", "Algenib": "남성 · 걸걸하고 굵음",
            "Schedar": "남성 · 고르고 담담함", "Sadaltager": "남성 · 지적이고 믿음직함", "Enceladus": "남성 · 숨결 섞인 낮은 톤",
        }
        cur = cfg.get("tts_voice", "Kore")
        names = list(VOICES) + ([cur] if cur not in VOICES else [])
        new["tts_voice"] = st.selectbox("목소리", names, index=names.index(cur),
                                        format_func=lambda v: f"{v} — {VOICES.get(v, '직접 입력한 목소리')}")
        new["tts_style"] = st.text_area("말투 지시", cfg["tts_style"])
        sample = st.text_input("들어 볼 문장", "그날 밤, 어머니는 아무 말 없이 밥상을 차리셨습니다. 그런데 그 밥그릇은… 아버지 것이었습니다.")
        sdir = config.ROOT / "voice_samples"; sdir.mkdir(exist_ok=True)
        if st.button("🔊 이 목소리로 들어 보기 (한 번에 1~2원)"):
            import hashlib
            key = hashlib.md5(f"{new['tts_voice']}|{new['tts_style']}|{sample}".encode()).hexdigest()[:10]
            out = sdir / f"{new['tts_voice']}_{key}.wav"
            try:
                if not out.exists():
                    with st.spinner("목소리 만드는 중…"):
                        ai.make_tts(dict(cfg, **new), sample, out)
                st.audio(str(out))
                st.caption("마음에 들면 아래 '💾 설정 저장'을 누르세요. 다음 영상부터 이 목소리로 나와요.")
            except Exception as e:
                st.error(f"오류: {e}")
    with st.expander("자막·음악"):
        new["font_path"] = st.text_input("자막 글꼴 파일", cfg["font_path"])
        new["subtitle_size"] = st.slider("자막 크기", 50, 110, int(cfg["subtitle_size"]))
        new["subtitle_y"] = st.slider("자막 높이 (0.5=정중앙)", 0.3, 0.8, float(cfg["subtitle_y"]))
        new["music_volume"] = st.slider("배경음악 음량", 0.05, 1.0, float(cfg["music_volume"]))
    if st.button("💾 설정 저장", type="primary"):
        config.save(new); st.session_state["saved_msg"] = True; st.rerun()
    if st.session_state.pop("saved_msg", False):
        st.success("✅ 저장했어요. (키는 이 컴퓨터에만 저장됩니다)")
    if st.button("🔌 구글 연결 시험 (쓸 수 있는 모델 목록 보기)"):
        try:
            ms = ai.list_google_models(new)
            st.success(f"연결 성공! 모델 {len(ms)}개")
            st.code("\n".join(m for m in ms if any(k in m for k in ["gemini", "veo", "imagen", "tts", "omni"])))
        except Exception as e:
            st.error(f"연결 실패: {e}")

# ---- 느린 곳 찾기용: 화면 하나 그리는 데 걸린 시간 ----
st.divider()
st.caption(f"⏱ 이 화면 준비 {(_T1 - _T0):.1f}초 + 그리기 {(time.time() - _T1):.1f}초  (느리면 이 숫자를 Claude에게 알려 주세요)")
