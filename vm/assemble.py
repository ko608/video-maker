"""영상 조립 — 장면 이어 붙이기, 자막(화면 중간·흰 글씨·검은 테두리), 배경음악, 썸네일.
무료 편집 도구 ffmpeg를 씁니다 (imageio-ffmpeg가 자동으로 설치해 줌)."""
import json, re, subprocess, wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30


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
    lines = [text] if d.textlength(text, font=font) < W - 80 else split_chunks(text, 9)
    y = int(H * float(cfg.get("subtitle_y", 0.55))) - (len(lines) * (size + 14)) // 2
    for ln in lines:
        w = d.textlength(ln, font=font)
        d.text(((W - w) / 2, y), ln, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
        y += size + 14
    img.save(out_path)


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
        dur = (wav_seconds(audio) + 0.5) if audio else 4.0

    # 자막 PNG (문장을 나눠 시간 비율대로)
    chunks = split_chunks(cut.get("text", ""))
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
                    f"tpad=stop_mode=clone:stop_duration={max(0, dur - vdur):.2f}[v0]")
    else:
        frames = int(dur * FPS)
        args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.2f}", "-i", str(media)]
        filt.append(f"[0:v]scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
                    f"zoompan=z='min(zoom+0.0007,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
                    f"setsar=1[v0]")
    n_in = 1
    # 오디오 입력
    if audio:
        args += ["-i", str(audio)]; a_idx = n_in; n_in += 1
        audio_src = f"[{a_idx}:a]aresample=44100,apad[a0]"
    elif is_video and media_info(media)[1]:
        audio_src = "[0:a]aresample=44100,apad[a0]"
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]; a_idx = n_in; n_in += 1
        audio_src = f"[{a_idx}:a]anull[a0]"
    filt.append(audio_src)
    # 자막 겹치기
    last = "v0"
    for i, (png, a, b) in enumerate(subs):
        args += ["-i", str(png)]
        nxt = f"v{i+1}"
        filt.append(f"[{last}][{n_in}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[{nxt}]")
        last = nxt; n_in += 1
    args += ["-filter_complex", ";".join(filt), "-map", f"[{last}]", "-map", "[a0]",
             "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", str(seg)]
    log(f"  장면 {idx} 편집 중 ({dur:.1f}초)")
    run(args)
    return seg, dur


# ---------------- 전체 조립 ----------------
def assemble(project: Path, cuts, cfg, music=None, log=print):
    work = project / "work"
    work.mkdir(exist_ok=True)
    segs = []
    for i, c in enumerate(cuts, 1):
        seg, _ = build_segment(work, i, c, cfg, log)
        segs.append(seg)
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


# ---------------- 썸네일 (밝은 배경 + 장면 사진) ----------------
def thumbnail(project: Path, lines, photo, cfg):
    import math
    font_path = cfg["font_path"]
    f = lambda s: ImageFont.truetype(font_path, s)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(255, int(226 - 20 * t), int(120 + 100 * t)))
    if photo and Path(photo).exists() and Path(photo).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        ph = Image.open(photo).convert("RGB")
        ph = ph.resize((W, int(ph.height * W / ph.width)))
        top = H - min(ph.height, 1000)
        img.paste(ph.crop((0, 0, W, min(ph.height, 1000))), (0, top))
        d = ImageDraw.Draw(img)
        d.rectangle([0, top - 8, W, top], fill=(220, 30, 30))
    def ctext(y, s, size, fill, stroke):
        while size > 40 and d.textlength(s, font=f(size)) > W - 70:
            size -= 4
        w = d.textlength(s, font=f(size))
        d.text(((W - w) / 2, y), s, font=f(size), fill=fill, stroke_width=stroke, stroke_fill="black")
    L = (list(lines) + ["", "", ""])[:3]
    if L[0]: ctext(180, L[0], 96, "white", 12)
    if L[1]: ctext(330, L[1], 136, (255, 235, 0), 14)
    if L[2]: ctext(530, L[2], 78, "white", 10)
    out = project / "썸네일.jpg"
    img.save(out, quality=92)
    return out
