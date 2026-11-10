"""영상 조립 — 장면 이어 붙이기, 자막(화면 중간·흰 글씨·검은 테두리), 배경음악, 썸네일.
무료 편집 도구 ffmpeg를 씁니다 (imageio-ffmpeg가 자동으로 설치해 줌)."""
import json, re, subprocess, wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30


def _brighten(img):
    from PIL import ImageEnhance
    img = img.convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(1.08)
    return ImageEnhance.Color(img).enhance(1.05)


def thumb_font(cfg):
    """썸네일은 아주 굵은 글씨(Noto Sans KR Black)로 — 프로그램 폴더에 같이 들어 있음"""
    f = Path(__file__).resolve().parent.parent / "fonts" / "NotoSansKR-Black-subset.otf"
    return str(f) if f.exists() else cfg["font_path"]


def set_format(fmt):
    """shorts = 세로 1080x1920, longform = 가로 1920x1080"""
    global W, H, FPS
    W, H = (1920, 1080) if fmt == "longform" else (1080, 1920)
    FPS = 15 if fmt == "longform" else 30    # 롱폼은 그림만 나와서 15프레임이면 충분 (편집 2배 빠름)


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def run(args, cwd=None):
    p = subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"] + args,
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("영상 편집 중 오류: " + p.stderr[-800:])


def media_info(path):
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = p.stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return dur, ("Audio:" in err)


def wav_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / float(w.getframerate())


# ---------------- 자막 ----------------
def split_chunks(text, limit=13):
    """자막을 한 줄 13자 안팎으로 자름 (띄어쓰기 기준)."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    out, cur = [], ""
    for word in text.split(" "):
        if cur and len(cur) + 1 + len(word) > limit:
            out.append(cur); cur = word
        else:
            cur = (cur + " " + word).strip()
        if re.search(r"[.?!…]$", cur) and len(cur) >= 6:
            out.append(cur); cur = ""
    if cur:
        out.append(cur)
    return out


def subtitle_png(text, out_path, cfg):
    size = int(cfg.get("subtitle_size", 76))
    font = ImageFont.truetype(cfg["font_path"], size) if cfg.get("font_path") else ImageFont.load_default()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = max(4, size // 12)
    lines = [text] if d.textlength(text, font=font) < W - 80 else split_chunks(text, max(9, len(text) // 2 + 1))
    y = int(H * float(cfg.get("subtitle_y", 0.55))) - (len(lines) * (size + 14)) // 2
    for ln in lines:
        w = d.textlength(ln, font=font)
        d.text(((W - w) / 2, y), ln, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
        y += size + 14
    img.save(out_path)


def _bright(cfg):
    """화면 밝기 보정 — AI 그림·영상이 어둡게 나와도 환하게 (설정의 '화면 밝게' 0~10)"""
    lv = float(cfg.get("brighten", 2))
    if lv <= 0:
        return ""
    g = 1 + 0.05 * lv          # 어두운 부분을 들어 올림 (5 → 1.25)
    b = 0.012 * lv             # 전체 밝기 (5 → 0.06)
    return f",eq=gamma={g:.2f}:brightness={b:.3f}:saturation=1.05"


def _img_size(path):
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def split_panels(path, n, out_dir, start_name="panel"):
    """여러 칸(위아래로 쌓인) 그림을 n장으로 나누기 — 칸 사이 경계를 찾아서 자름"""
    im = Image.open(path).convert("RGB")
    w, h = im.size
    gray = im.convert("L").resize((max(1, w // 8), h))
    # 줄마다 밝기 변화가 큰 곳(칸 경계) 찾기 — 못 찾으면 똑같이 나눔
    import statistics
    rows = [list(gray.crop((0, y, gray.width, y + 1)).getdata()) for y in range(h)]
    diff = [0] + [sum(abs(a - b) for a, b in zip(rows[y], rows[y - 1])) for y in range(1, h)]
    cuts = []
    for k in range(1, n):
        guess = h * k // n
        lo, hi = max(1, guess - h // (n * 6)), min(h - 1, guess + h // (n * 6))
        y = max(range(lo, hi), key=lambda yy: diff[yy])
        cuts.append(y if diff[y] > statistics.mean(diff) * 4 else guess)
    bounds = [0] + cuts + [h]
    outs = []
    for i in range(n):
        piece = im.crop((0, bounds[i] + 2, w, bounds[i + 1] - 2))
        o = Path(out_dir) / f"{start_name}_{i + 1}.png"
        piece.save(o); outs.append(o)
    return outs


# ---------------- 장면 하나 만들기 ----------------
def build_segment(work: Path, idx, cut, cfg, log=print):
    """cut: kind, media(파일), audio(나레이션 wav 또는 없음), text(자막 문장)"""
    seg = work / f"seg_{idx:02d}.mp4"
    media = Path(cut["media"])
    is_video = media.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")
    audio = Path(cut["audio"]) if cut.get("audio") else None

    if is_video:
        vdur, has_a = media_info(media)
        dur = vdur
        if audio:
            dur = max(vdur, wav_seconds(audio) + 0.3)
    else:
        dur = (wav_seconds(audio) + 0.5) if audio else max(4.0, len(cut.get("text", "")) / 7)   # 목소리 없으면 자막 읽을 시간만큼

    # 자막 PNG (문장을 나눠 시간 비율대로)
    chunks = split_chunks(cut.get("text", ""), int(cfg.get("subtitle_chars", 13)))
    total = sum(len(c) for c in chunks) or 1
    t, subs = 0.15, []
    for i, c in enumerate(chunks):
        span = (dur - 0.3) * len(c) / total
        png = work / f"sub_{idx:02d}_{i:02d}.png"
        subtitle_png(c, png, cfg)
        subs.append((png, t, t + span))
        t += span

    args, filt = [], []
    if is_video:
        args += ["-i", str(media)]
        filt.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,"
                    f"tpad=stop_mode=clone:stop_duration={max(0, dur - vdur):.2f}{_bright(cfg)}[v0]")
    else:
        # 그림 컷: 그림이 여러 장이면 나눠서 번갈아 보여 줌 (5~6초마다 화면 바뀜) + 천천히 움직임
        imgs = [media] + [Path(x) for x in cut.get("extras", []) if Path(x).exists()]
        k = len(imgs)
        part = dur / k
        motion = cfg.get("image_motion", True)
        labels = []
        for j, im in enumerate(imgs):
            fr = max(1, int(part * FPS))
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{part:.3f}", "-i", str(im)]
            iw, ih = _img_size(im)
            wide = iw and ih and (iw / ih) / (W / H) > 1.4
            if wide:   # 가로 그림을 세로 영상에 → 가운데 크게 + 뒤는 흐린 배경
                base = (f"[{j}:v]split=2[bg{j}][fg{j}];"
                        f"[bg{j}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=0.06[bgs{j}];"
                        f"[fg{j}]scale={W}:-2[fgs{j}];[bgs{j}][fgs{j}]overlay=0:(H-h)/2[b{j}]")
            else:
                base = f"[{j}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}[b{j}]"
            if motion:   # 확대·축소를 번갈아 (지루하지 않게)
                zin = j % 2 == 0
                zexpr = (f"min(1+{0.10 / fr:.6f}*on,1.10)" if zin else f"max(1.10-{0.10 / fr:.6f}*on,1.0)")
                filt.append(base + f";[b{j}]scale={int(W*1.2)//2*2}:{int(H*1.2)//2*2},"
                            f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
                            f"trim=duration={part:.3f},setpts=PTS-STARTPTS,setsar=1[p{j}]")
            else:
                filt.append(base + f";[b{j}]fps={FPS},setsar=1[p{j}]")
            labels.append(f"[p{j}]")
        filt.append("".join(labels) + f"concat=n={k}:v=1:a=0,fps={FPS},format=yuv420p{_bright(cfg)}[v0]")
    n_in = (1 if is_video else len(imgs))
    # 오디오 입력
    if audio:
        args += ["-i", str(audio)]; a_idx = n_in; n_in += 1
        audio_src = (f"[{a_idx}:a]aresample=44100,"
                     "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,"
                     "afade=t=in:d=0.03,apad[a0x]")
    elif is_video and media_info(media)[1]:
        audio_src = "[0:a]aresample=44100,apad[a0x]"
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]; a_idx = n_in; n_in += 1
        audio_src = f"[{a_idx}:a]anull[a0x]"
    filt.append(audio_src)
    # 장면 끝 0.25초 부드럽게 줄이기 → 컷 이음새 '딱'·'지직' 잡음 없애기
    filt.append(f"[a0x]atrim=0:{dur:.2f},afade=t=out:st={max(0, dur-0.25):.2f}:d=0.25[a0]")
    # 자막 겹치기
    last = "v0"
    for i, (png, a, b) in enumerate(subs):
        args += ["-i", str(png)]
        nxt = f"v{i+1}"
        filt.append(f"[{last}][{n_in}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[{nxt}]")
        last = nxt; n_in += 1
    args += ["-filter_complex", ";".join(filt), "-map", f"[{last}]", "-map", "[a0]",
             "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "superfast", "-crf", "21", "-pix_fmt", "yuv420p",
             *(["-tune", "stillimage"] if not is_video and not cfg.get("image_motion", False) else []),
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", str(seg)]
    log(f"  장면 {idx} 편집 중 ({dur:.1f}초)")
    run(args)
    return seg, dur


# ---------------- 전체 조립 ----------------
def assemble(project: Path, cuts, cfg, music=None, log=print, durs=None):
    work = project / "work"
    work.mkdir(exist_ok=True)
    # 장면들을 여러 개 동시에 편집 (컴퓨터 코어 수에 맞춰)
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    workers = max(1, min(4, (os.cpu_count() or 2) // 2))
    log(f"  장면 {len(cuts)}개를 {workers}개씩 동시에 편집하는 중…")
    results = [None] * len(cuts)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(build_segment, work, i, c, cfg, lambda m: None): i for i, c in enumerate(cuts, 1)}
        k = 0
        for fu in as_completed(futs):
            k += 1
            results[futs[fu] - 1] = fu.result()
            log(f"  ✔ 장면 편집 {k}/{len(cuts)}")
    segs = [r[0] for r in results]
    if durs is not None:
        durs.extend(r[1] for r in results)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{s.name}'\n" for s in segs), encoding="utf-8")
    joined = work / "joined.mp4"
    log("  장면 이어 붙이는 중")
    run(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=work)
    final = project / "완성본.mp4"
    if music and Path(music).exists():
        dur, _ = media_info(joined)
        vol = float(cfg.get("music_volume", 0.35))
        log("  배경음악 넣는 중 (나레이션 나올 때 자동으로 작아짐)")
        run(["-i", str(joined), "-stream_loop", "-1", "-i", str(music), "-filter_complex",
             f"[0:a]asplit=2[voice][key];[1:a]volume={vol},afade=t=out:st={max(0, dur-2):.2f}:d=2[m];"
             f"[m][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=500[duck];"
             f"[voice][duck]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-t", f"{dur:.2f}", str(final)])
    else:
        run(["-i", str(joined), "-c", "copy", str(final)])
    log("✅ 완성: " + str(final))
    return final


# ---------------- 썸네일 (사진 꽉 차게 + 큰 글씨 겹치기) ----------------
def thumbnail_wide(project: Path, lines, photo, cfg, out_name="썸네일_A.jpg"):
    """롱폼(가로) 썸네일: 사진 꽉 차게, 글씨는 얼굴 아래(화면 아래쪽)에 흰·노란 큰 글씨 2줄"""
    font_path = thumb_font(cfg)
    f = lambda sz: ImageFont.truetype(font_path, sz)
    TW, TH = 1280, 720
    if photo and Path(photo).exists():
        ph = Image.open(photo).convert("RGB")
        sc = max(TW / ph.width, TH / ph.height)
        ph = ph.resize((int(ph.width * sc) + 1, int(ph.height * sc) + 1), Image.LANCZOS)
        x = (ph.width - TW) // 2
        y = int((ph.height - TH) * 0.2)          # 얼굴이 있는 위쪽을 살림
        img = ph.crop((x, y, x + TW, y + TH))
    else:
        img = Image.new("RGB", (TW, TH), (255, 214, 90))
    # 아래쪽만 살짝 어둡게 → 글씨 또렷, 얼굴은 밝게 그대로
    shade = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shade)
    top = int(TH * 0.5)
    for yy in range(top, TH):
        a_ = int(170 * ((yy - top) / (TH - top)) ** 1.3)
        ds.line([(0, yy), (TW, yy)], fill=(0, 0, 0, a_))
    img = _brighten(img)   # 글씨 아래를 어둡게 하지 않고, 전체를 살짝 밝게 (글씨는 굵은 테두리로 또렷하게)
    d = ImageDraw.Draw(img)

    def fit(s_, size):
        while size > 40 and d.textlength(s_, font=f(size)) > TW - 60:
            size -= 4
        return size

    L = [x_ for x_ in (list(lines) + ["", ""])[:2]]
    s2 = fit(L[1], 118) if L[1] else 0
    s1 = fit(L[0], 92) if L[0] else 0
    y2 = TH - s2 - 58
    y1 = y2 - s1 - 22
    if L[0]:
        w_ = d.textlength(L[0], font=f(s1))
        d.text(((TW - w_) / 2, y1), L[0], font=f(s1), fill="white", stroke_width=13, stroke_fill="black")
    if L[1]:
        w_ = d.textlength(L[1], font=f(s2))
        d.text(((TW - w_) / 2, y2), L[1], font=f(s2), fill=(255, 225, 0), stroke_width=15, stroke_fill="black")
    out = project / out_name
    img.save(out, quality=95, subsampling=0)
    return out


def thumbnail_panel(project: Path, lines, photo, cfg, out_name="썸네일_B.jpg"):
    """롱폼(가로) 썸네일: 왼쪽 밝은 황금색 글씨판 + 오른쪽 장면 사진"""
    font_path = thumb_font(cfg)
    f = lambda sz: ImageFont.truetype(font_path, sz)
    TW, TH = 1280, 720
    PW = 460                                   # 왼쪽 글씨판 너비 (사진 넓게)
    img = Image.new("RGB", (TW, TH))
    d = ImageDraw.Draw(img)
    for xx in range(PW + 40):                  # 밝은 황금색 → 연노랑
        t = xx / (PW + 40)
        d.line([(xx, 0), (xx, TH)], fill=(255, int(200 + 40 * t), int(40 + 120 * t)))
    if photo and Path(photo).exists():
        ph = Image.open(photo).convert("RGB")
        RW = TW - PW
        sc = max(RW / ph.width, TH / ph.height)
        ph = ph.resize((int(ph.width * sc) + 1, int(ph.height * sc) + 1), Image.LANCZOS)
        x = (ph.width - RW) // 2
        y = int((ph.height - TH) * 0.3)
        img.paste(ph.crop((x, y, x + RW, y + TH)), (PW, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([PW - 6, 0, PW + 2, TH], fill=(220, 30, 30))   # 빨간 경계선

    def wrap(s, size, maxw):
        words, out, cur = s.split(), [], ""
        for w_ in words:
            t = (cur + " " + w_).strip()
            if cur and d.textlength(t, font=f(size)) > maxw:
                out.append(cur); cur = w_
            else:
                cur = t
        if cur:
            out.append(cur)
        return out

    L = (list(lines) + ["", "", ""])[:3]
    # 글자는 적게: 작은 윗줄 + 크고 빨간 핵심 한 줄 (3번째 줄은 뺌)
    spec = [(L[0], 58, (30, 30, 30), "white", 6), (L[1], 130, (225, 25, 25), "white", 11)]
    blocks = []
    for txt, size, fill, sf, st_ in spec:
        if not txt:
            continue
        rows = wrap(txt, size, PW - 75)
        while size > 36 and (len(rows) > 2 or max(d.textlength(r, font=f(size)) for r in rows) > PW - 60):
            size -= 6; rows = wrap(txt, size, PW - 75)
        blocks.append((rows, size, fill, sf, st_))
    total = sum(len(r) * (sz + 12) for r, sz, *_ in blocks) + 30 * (len(blocks) - 1)
    y = max(20, (TH - total) // 2)
    for rows, size, fill, sf, st_ in blocks:
        for r in rows:
            d.text((30, y), r, font=f(size), fill=fill, stroke_width=st_, stroke_fill=sf)
            y += size + 12
        y += 30
    out = project / out_name
    img.save(out, quality=95, subsampling=0)
    return out




def thumbnail_tall_low(project: Path, lines, photo, cfg, out_name="썸네일_A.jpg"):
    """쇼츠(세로) 썸네일 A: 사진 꽉 차게, 얼굴 아래(화면 60~80% 높이)에 흰·노란 큰 글씨 2줄"""
    font_path = thumb_font(cfg)
    f = lambda sz: ImageFont.truetype(font_path, sz)
    TW, TH = 1080, 1920
    if photo and Path(photo).exists():
        ph = Image.open(photo).convert("RGB")
        sc = max(TW / ph.width, TH / ph.height)
        ph = ph.resize((int(ph.width * sc) + 1, int(ph.height * sc) + 1), Image.LANCZOS)
        x = (ph.width - TW) // 2
        y = int((ph.height - TH) * 0.2)
        img = ph.crop((x, y, x + TW, y + TH))
    else:
        img = Image.new("RGB", (TW, TH), (255, 214, 90))
    shade = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shade)
    top = int(TH * 0.5)
    for yy in range(top, TH):
        a_ = int(160 * min(1, (yy - top) / (TH * 0.3)) ** 1.2)
        ds.line([(0, yy), (TW, yy)], fill=(0, 0, 0, a_))
    img = _brighten(img)   # 글씨 아래를 어둡게 하지 않고, 전체를 살짝 밝게 (글씨는 굵은 테두리로 또렷하게)
    d = ImageDraw.Draw(img)

    def fit(s_, size):
        while size > 50 and d.textlength(s_, font=f(size)) > TW - 60:
            size -= 4
        return size

    L = (list(lines) + ["", ""])[:2]
    s1 = fit(L[0], 112) if L[0] else 0
    s2 = fit(L[1], 150) if L[1] else 0
    y1 = int(TH * 0.62)
    if L[0]:
        w_ = d.textlength(L[0], font=f(s1))
        d.text(((TW - w_) / 2, y1), L[0], font=f(s1), fill="white", stroke_width=16, stroke_fill="black")
    if L[1]:
        y2 = y1 + (s1 + 34 if L[0] else 0)
        w_ = d.textlength(L[1], font=f(s2))
        d.text(((TW - w_) / 2, y2), L[1], font=f(s2), fill=(255, 225, 0), stroke_width=18, stroke_fill="black")
    out = project / out_name
    img.save(out, quality=95, subsampling=0)
    return out


def thumbnails(project: Path, lines, photos, cfg):
    """썸네일 2가지: A(글씨 아래) · B(롱폼=왼쪽 글씨판 / 쇼츠=다른 장면) — 대표 썸네일.jpg는 A"""
    import shutil
    photos = [x for x in (photos or []) if x] or [None]
    pa, pb = photos[0], (photos[1] if len(photos) > 1 else photos[0])
    # A·B 모두 '글씨 아래' 모양, 서로 다른 핵심 장면 2곳
    if W > H:
        a = thumbnail_wide(project, lines, pa, cfg, "썸네일_A.jpg")
        b = thumbnail_wide(project, lines, pb, cfg, "썸네일_B.jpg")
    else:
        a = thumbnail_tall_low(project, lines, pa, cfg, "썸네일_A.jpg")
        b = thumbnail_tall_low(project, lines, pb, cfg, "썸네일_B.jpg")
    shutil.copy(a, project / "썸네일.jpg")
    return a, b


def thumbnail(project: Path, lines, photo, cfg):
    if W > H:
        return thumbnail_wide(project, lines, photo, cfg, "썸네일.jpg")
    font_path = thumb_font(cfg)
    f = lambda sz: ImageFont.truetype(font_path, sz)
    img = None
    if photo and Path(photo).exists() and Path(photo).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        ph = Image.open(photo).convert("RGB")
        sc = max(W / ph.width, H / ph.height)
        ph = ph.resize((int(ph.width * sc) + 1, int(ph.height * sc) + 1), Image.LANCZOS)
        if sc > 1.2:   # 작은 그림을 키울 땐 살짝 또렷하게
            from PIL import ImageFilter
            ph = ph.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
        x = (ph.width - W) // 2
        y = int((ph.height - H) * 0.3)          # 얼굴이 있는 위쪽을 조금 더 살림
        img = ph.crop((x, y, x + W, y + H))
    if img is None:                              # 사진이 없으면 밝은 바탕
        img = Image.new("RGB", (W, H))
        d0 = ImageDraw.Draw(img)
        for yy in range(H):
            t = yy / H
            d0.line([(0, yy), (W, yy)], fill=(255, int(226 - 20 * t), int(120 + 100 * t)))
    # 글씨 뒤만 살짝 어둡게 (위쪽 → 투명) — 사진은 밝게 살리고 글씨는 또렷하게
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shade)
    SH = min(1000, int(H * 0.65))
    for yy in range(0, SH):
        a = int(120 * (1 - yy / SH) ** 1.5)
        ds.line([(0, yy), (W, yy)], fill=(0, 0, 0, a))
    img = _brighten(img)   # 글씨 아래를 어둡게 하지 않고, 전체를 살짝 밝게 (글씨는 굵은 테두리로 또렷하게)
    d = ImageDraw.Draw(img)

    def ctext(y, s, size, fill, stroke):
        while size > 40 and d.textlength(s, font=f(size)) > W - 60:
            size -= 4
        w = d.textlength(s, font=f(size))
        d.text(((W - w) / 2, y), s, font=f(size), fill=fill, stroke_width=stroke, stroke_fill="black")
        return size

    L = (list(lines) + ["", "", ""])[:3]
    y = 170
    if L[0]:
        sz = ctext(y, L[0], 115, "white", 13); y += sz + 45
    if L[1]:
        sz = ctext(y, L[1], 165, (255, 230, 0), 18); y += sz + 50
    if L[2]:
        ctext(y, L[2], 95, "white", 12)
    out = project / "썸네일.jpg"
    img.save(out, quality=95, subsampling=0)
    return out
