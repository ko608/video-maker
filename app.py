"""영상 자판기 — 대표님 전용 영상 자동 제작 프로그램 (쇼츠 1단계)
실행: 실행하기.bat 더블클릭 → 인터넷 창이 열립니다."""
import json, os, re, shutil, subprocess, sys, time
from datetime import datetime
from pathlib import Path

import streamlit as st

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


def channels():
    out = {}
    for f in sorted(config.CHANNELS.glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        out[c["name"]] = c
    return out


def projects():
    return sorted([p for p in config.PROJECTS.iterdir() if (p / "script.json").exists()], reverse=True)


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


class Log:
    def __init__(self):
        self.box = st.empty(); self.lines = []
    def __call__(self, s):
        self.lines.append(str(s)); self.box.code("\n".join(self.lines[-14:]))


# 탭 대신 고르기 버튼 — 고른 화면만 그려서 가볍게 (st.tabs는 네 화면을 매번 다 그림)
PAGES = ["① 분석·대본", "② 재료 만들기", "③ 완성 영상", "⚙️ 설정"]
page = st.radio("화면", PAGES, horizontal=True, label_visibility="collapsed", key="page")
st.divider()

# ================= ① 분석·대본 =================
if page == "① 분석·대본":
    chs = channels()
    ch_name = st.selectbox("우리 채널", list(chs))
    ch = dict(chs[ch_name])
    if ch.get("format") == "longform":
        mins = st.select_slider("⏱ 영상 길이 (분)", options=[5, 8, 10, 15, 20, 25, 30], value=10,
                                help="길수록 그림·목소리 개수가 늘어 비용과 만드는 시간이 늘어요")
        ch["cuts"] = max(10, round(mins * 60 / 28))          # 장면 1개 ≈ 25~30초
        ch["chapters"] = max(3, min(10, mins // 3 + 1))
        st.caption(f"→ 장면 약 {ch['cuts']}개, {ch['chapters']}개 장으로 써요. "
                   f"(자동 그림이면 대략 {ch['cuts'] * 70:,}~{ch['cuts'] * 130:,}원)")
    else:
        st.caption("⏱ 쇼츠는 약 55초(6컷)로 고정이에요.")
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
    topic = st.text_area("주제·메모 (선택)", placeholder="예: 시어머니가 며느리 몰래 들어 둔 보험. 돈 + 사주 반전",
                         height=80, key="topic_box")

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
        data = load_script(p)
        fmt = data.get("format", "shorts")
        aspect = "16:9" if fmt == "longform" else "9:16"
        ratio_ko = "가로 16:9" if fmt == "longform" else "세로 9:16"
        chs_by_id = {c["id"]: c for c in channels().values()}
        style = chs_by_id.get(data.get("channel"), {}).get("visual_style", "photorealistic, vertical 9:16, no text")
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
                                       "Ordinary everyday Korean people with average, natural faces. "
                                       "Bright, airy, high-key lighting, no text.", ref, aspect=aspect)
            except Exception as e:
                st.error(f"오류: {e}")
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
                    if c["kind"] != "image" or cut_file(p, no):
                        continue
                    base = (f"Scene {no}: {c['visual']}. Style: {style}. Characters: {data.get('characters','')}. "
                            "Ordinary everyday Korean people with average, natural faces, light natural makeup, "
                            "realistic skin texture")
                    jobs[pool.submit(ai.make_image, cfg, base, p / "재료" / f"{no:02d}.png", aspect, ref)] = no
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
                        "realistic skin texture")
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
                                          "face clearly visible, about to speak.", still, ref=ref, aspect=aspect)
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
                                   "Natural lip sync, no background music, no subtitles, no text on screen.")
                        ai.make_video(cfg, vprompt, p / "재료" / f"{no:02d}.mp4", first_frame=still, log=log)
                except Exception as e:
                    log(f"컷 {no} 실패: {e}")
            log("끝! 아래에서 확인하세요.")
        st.divider()

        # ---- 플로우(무료) 한 번에: 전체 문구 + 한꺼번에 올리기 ----
        st.markdown("### 🆓 플로우로 한 번에 만들기 (무료)")
        common = (f"{ratio_ko}, 화면은 밝게(햇살·환한 조명), 배경·건물·옷은 이야기 시대와 장소 그대로(옛날 초가 주막이면 허름한 초가 그대로 — 더 부유하거나 현대식으로 바꾸지 말 것), 글자·자막 없이. 평범한 일반인 얼굴(화장 옅게, "
                  f"자연스러운 피부). 모든 장면에서 같은 인물: {data.get('characters','')}")
        lines_all = [f"아래 그림들을 순서대로 만들어 줘. 첨부한 인물 기준 그림과 똑같은 사람(얼굴·머리·옷)으로 — 기준 그림의 왼쪽부터 등장인물 설명 순서대로. 여러 명이 나오는 장면도 각자 기준 그림의 그 사람으로. 공통: {common}", ""]
        for c in data["cuts"]:
            if c["kind"] == "video":
                continue   # 대사 영상은 컷마다 시작 그림을 넣고 아래 짧은 문구로 따로 만들기
            else:
                lines_all.append(f"{c['no']}번 [이미지] {c['visual']}")
        st.text_area("① 그림 컷 문구 전체 복사 → 플로우 채팅창에 한 번에 (대사 영상은 아래 컷마다 짧은 문구로)",
                     "\n".join(lines_all), height=220, key="flow_all")
        empty = [int(c["no"]) for c in data["cuts"] if not cut_file(p, int(c["no"]))]
        ups = st.file_uploader(f"② 플로우에서 받은 파일들을 한꺼번에 올리기 — 빈 컷 {empty} 에 "
                               "그림은 그림 컷, 영상은 대사 컷 순서대로 들어가요 (파일 이름 순)",
                               type=["png", "jpg", "jpeg", "webp", "mp4", "mov"],
                               accept_multiple_files=True, key="bulk_up")
        if ups and st.button("📥 올린 파일들 컷에 넣기", type="primary"):
            vids = sorted([u for u in ups if Path(u.name).suffix.lower() in (".mp4", ".mov")], key=lambda u: u.name)
            pics = sorted([u for u in ups if u not in vids], key=lambda u: u.name)
            done = []
            for c in data["cuts"]:
                no = int(c["no"])
                if cut_file(p, no):
                    continue
                pool = vids if c["kind"] == "video" else pics
                if not pool:
                    continue
                u = pool.pop(0)
                (p / "재료" / f"{no:02d}{Path(u.name).suffix.lower()}").write_bytes(u.getbuffer())
                done.append(no)
            st.success(f"컷 {done} 에 넣었어요. 순서가 틀린 컷은 아래에서 지우고 다시 올리세요.")
            st.rerun()
        st.divider()
        for c in data["cuts"]:
            no = int(c["no"])
            f = cut_file(p, no)
            l, m, r = st.columns([2, 3, 2])
            l.markdown(f"### 컷 {no} · {'🎥 대사 영상' if c['kind']=='video' else '🖼️ 이미지+나레이션'}")
            if c.get("chapter"):
                l.markdown(f"**📖 {c['chapter']}**")
            l.write(c.get("line") or c.get("narration", ""))
            if c["kind"] == "video":
                # 플로우 영상은 짧게 — 시작 그림이 인물·배경을 이미 담고 있음 (긴 문구는 거절되기 쉬움)
                flow = (f"첨부한 시작 그림에서 이어지는 4초 영상. {c['visual']}\n"
                        f"{c.get('speaker','')} 말함: \"{c['line']}\"\n"
                        "다른 사람은 말하지 않음. 밝고 환한 분위기, 자막 없이.")
            else:
                flow = (f"[컷 {no} 장면] {c['visual']}"
                        f"\n[공통] {ratio_ko}, 화면은 밝게(햇살·환한 조명), 배경·건물·옷은 이야기 시대와 장소 그대로 "
                        "(더 부유하거나 현대식으로 바꾸지 말 것), 글자·자막 없이. 평범한 일반인 얼굴(화장 옅게, 자연스러운 피부). "
                        + f"등장인물: {data.get('characters','')}")
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
                if r.button("🗑 이 재료 지우기", key=f"del{no}"):
                    f.unlink(); st.rerun()
            else:
                still = p / "재료" / f"{no:02d}_첫장면.png"
                if c["kind"] == "video" and still.exists():
                    show_small(r, still, caption="첫 장면 그림 (플로우 시작 이미지)")
                    r.download_button("⬇ 첫 장면 그림 받기", still.read_bytes(), file_name=f"컷{no}_첫장면.png",
                                      mime="image/png", key=f"dl{no}")
                    r.warning("대사 영상을 플로우에서 만들어 올려 주세요")
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
        if st.button("🎬 영상 만들기", type="primary", disabled=bool(missing), width="stretch"):
            log = Log()
            try:
                # 나레이션 목소리를 4개씩 동시에 미리 만들기
                from concurrent.futures import ThreadPoolExecutor, as_completed
                (p / "work").mkdir(exist_ok=True)
                need = [c for c in data["cuts"] if c["kind"] == "image" and c.get("narration")
                        and not (own_voice and list((p / "재료").glob(f"{int(c['no']):02d}_나레이션.*")))
                        and not (p / "work" / f"voice2_{int(c['no']):02d}.wav").exists()]
                if need:
                    log(f"나레이션 목소리 {len(need)}개를 동시에 만드는 중…")
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        futs = {pool.submit(ai.make_tts, cfg, c["narration"], p / "work" / f"voice2_{int(c['no']):02d}.wav"): c["no"] for c in need}
                        k = 0
                        for fu in as_completed(futs):
                            k += 1
                            fu.result(); log(f"  ✔ 목소리 {futs[fu]} ({k}/{len(need)})")
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
                            wav = p / "work" / f"voice2_{no:02d}.wav"
                            (p / "work").mkdir(exist_ok=True)
                            if not wav.exists():
                                log(f"컷 {no}: 나레이션 목소리 만드는 중")
                                ai.make_tts(cfg, c["narration"], wav)
                            audio = wav
                    cuts.append(dict(media=media, audio=audio, text=c.get("line") or c.get("narration", "")))
                assemble.set_format(fmt)
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
                # 썸네일 사진: 재료 폴더의 그림 중 가장 해상도 높은 원본
                from PIL import Image as _I
                pics = [x for x in (p / "재료").iterdir() if x.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                        and "기준" not in x.name]
                def _px(x):
                    try:
                        w_, h_ = _I.open(x).size; return w_ * h_
                    except Exception:
                        return 0
                photo = max(pics, key=_px) if pics else None
                assemble.thumbnail(p, data.get("thumbnail", []), photo, cfg)
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
            if (p / "썸네일.jpg").exists():
                a.image(str(p / "썸네일.jpg"), caption="썸네일", width=260)
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
    new["image_size"] = st.radio("그림 해상도 (자동 그림)", ["1K", "2K"], horizontal=True,
                                 index=["1K", "2K"].index(cfg.get("image_size", "2K")) if cfg.get("image_size", "2K") in ("1K", "2K") else 1,
                                 help="1K = 비용 절약(롱폼 추천), 2K = 더 선명(쇼츠 썸네일용)")
    with st.expander("목소리·자막·음악"):
        new["tts_voice"] = st.text_input("목소리 이름 (Kore, Aoede, Charon, Puck 등)", cfg["tts_voice"])
        new["tts_style"] = st.text_area("말투 지시", cfg["tts_style"])
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
