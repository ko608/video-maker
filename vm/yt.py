"""📈 터진 영상 찾기 · 이탈 분석
- 터진 영상: 유튜브에서 주제로 찾은 영상을 '그 채널 평소 조회수(중앙값)의 몇 배'로 줄 세웁니다.
  구독자 많은 채널이 아니라 자기 평소보다 터진 영상이라, 작은 채널도 따라 할 공식이 보여요.
- 이탈 분석: 유튜브 스튜디오에서 받은 시청 지속(CSV)을 읽고 어디서 빠졌는지 + 그 순간 무슨 장면이었는지 봅니다.
유튜브 데이터는 공개된 정보만 읽습니다(로그인·업로드 없음). 'YouTube Data API v3' 키가 필요해요 (무료, 하루 1만 점).
"""
import csv, io, json, re, statistics, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://www.googleapis.com/youtube/v3/"


def _key(cfg):
    k = (cfg.get("youtube_api_key") or cfg.get("google_api_key") or "").strip()
    if not k:
        raise RuntimeError("설정에서 유튜브 키(YouTube Data API v3)를 넣어 주세요.")
    return k


def _get(cfg, path, **params):
    params["key"] = _key(cfg)
    url = API + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if "quotaExceeded" in body:
            raise RuntimeError("오늘 유튜브 검색 한도(하루 1만 점)를 다 썼어요. 내일 다시 해 주세요.")
        if "accessNotConfigured" in body or "SERVICE_DISABLED" in body or "has not been used" in body:
            raise RuntimeError("이 키에서 'YouTube Data API v3'가 꺼져 있어요. 설정 화면 안내대로 켜 주세요.")
        if e.code in (400, 403) and ("API key not valid" in body or "API_KEY_INVALID" in body):
            raise RuntimeError("유튜브 키가 맞지 않아요. 설정에서 다시 넣어 주세요.")
        raise RuntimeError(f"유튜브 응답 오류 {e.code}: {body[:200]}")


def _secs(iso):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _videos(cfg, ids):
    out = {}
    for i in range(0, len(ids), 50):
        r = _get(cfg, "videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]), maxResults=50)
        for v in r.get("items", []):
            out[v["id"]] = {
                "id": v["id"], "title": v["snippet"]["title"], "channel": v["snippet"]["channelTitle"],
                "channel_id": v["snippet"]["channelId"], "published": v["snippet"]["publishedAt"][:10],
                "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
                "secs": _secs(v.get("contentDetails", {}).get("duration")),
                "thumb": (v["snippet"].get("thumbnails", {}).get("medium") or {}).get("url", ""),
            }
    return out


def channel_median(cfg, channel_id, n=30, shorts=None):
    """채널 최근 영상 n개의 조회수 중앙값 (쇼츠만/롱폼만 따로 비교 가능)"""
    r = _get(cfg, "channels", part="contentDetails,statistics", id=channel_id)
    items = r.get("items") or []
    if not items:
        return 0, 0
    up = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    subs = int(items[0].get("statistics", {}).get("subscriberCount", 0) or 0)
    pl = _get(cfg, "playlistItems", part="contentDetails", playlistId=up, maxResults=50)
    ids = [x["contentDetails"]["videoId"] for x in pl.get("items", [])]
    vs = list(_videos(cfg, ids).values())
    # 갓 올린 영상(3일 이내)은 아직 조회수가 덜 쌓여서 빼고 계산
    cut = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    vs = [v for v in vs if v["published"] <= cut]
    if shorts is not None:
        same = [v for v in vs if (v["secs"] <= 180) == shorts]
        if len(same) >= 5:
            vs = same
    views = [v["views"] for v in vs[:n]]
    return (statistics.median(views) if views else 0), subs


def find_viral(cfg, keyword, months=3, shorts=True, min_ratio=2.0, log=print):
    """주제로 찾은 영상 → 채널 평소(중앙값) 대비 몇 배인지 계산해서 줄 세우기"""
    after = (datetime.now(timezone.utc) - timedelta(days=int(months * 30))).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = []
    for order in ("viewCount", "relevance"):        # 조회수순 + 관련도순 두 번 찾아서 놓치는 영상 줄이기
        log(f"유튜브에서 '{keyword}' 찾는 중… ({'조회수순' if order == 'viewCount' else '관련도순'})")
        q = dict(part="id", q=keyword, type="video", order=order, publishedAfter=after,
                 regionCode="KR", relevanceLanguage="ko", maxResults=50)
        if shorts:
            q["videoDuration"] = "short"             # 4분 미만
        r = _get(cfg, "search", **q)
        ids += [x["id"]["videoId"] for x in r.get("items", []) if x.get("id", {}).get("videoId")]
    ids = list(dict.fromkeys(ids))
    vids = _videos(cfg, ids)
    if shorts:
        vids = {k: v for k, v in vids.items() if v["secs"] <= 180}
    chans = list(dict.fromkeys(v["channel_id"] for v in vids.values()))
    med = {}
    for i, c in enumerate(chans, 1):
        log(f"채널 평소 조회수 계산 중… {i}/{len(chans)}")
        try:
            med[c] = channel_median(cfg, c, shorts=shorts)
        except Exception:
            med[c] = (0, 0)
    rows = []
    for v in vids.values():
        m, subs = med.get(v["channel_id"], (0, 0))
        if m <= 0 or v["views"] < 1000:
            continue
        v = dict(v, median=int(m), subs=subs, ratio=round(v["views"] / m, 1),
                 url=(f"https://www.youtube.com/shorts/{v['id']}" if v["secs"] <= 180 else f"https://www.youtube.com/watch?v={v['id']}"))
        rows.append(v)
    rows.sort(key=lambda x: x["ratio"], reverse=True)
    hits = [r for r in rows if r["ratio"] >= min_ratio]
    return hits, len(vids), len(chans)


def name_formulas(cfg, keyword, hits, channel_desc=""):
    """터진 영상 제목에 '공식 이름'을 붙이고, 우리 채널에 쓸 공식 3개 고르기 (클로드)"""
    from vm import ai
    lines = "\n".join(f"{i+1}. [{h['ratio']}배 · {h['views']:,}회] {h['title']}" for i, h in enumerate(hits[:40]))
    prompt = f"""아래는 유튜브에서 '{keyword}'로 찾은 영상 중, 그 채널 평소 조회수보다 크게 터진 영상 제목이에요 (몇 배인지 앞에 표시).

{lines}

우리 채널: {channel_desc or '(설명 없음)'}

할 일 (한국어로, 60대 사장님이 읽기 쉽게):
1. 제목들을 '제목 공식'별로 묶어 주세요. 공식 이름은 짧게(예: "숨은 이유 반전형", "숫자 경고형", "OO하면 안 되는 이유형"). 각 공식에 해당 번호들과 공통점 한 줄.
2. 우리 채널에 바로 쓸 공식 3개를 골라, 공식마다 우리 채널용 제목 예시 2개씩.
3. 주의: 공식 이름은 제목 문구로 나눈 것일 뿐, 그게 터진 이유라고 단정하지 마세요. 숫자는 위에 있는 것만 쓰고 지어내지 마세요.

JSON만 답하세요:
{{"formulas":[{{"name":"","nos":[1,2],"common":""}}],"picks":[{{"name":"","why":"","titles":["",""]}}]}}"""
    return ai._json_from(ai._claude(cfg, prompt, max_tokens=4000))


# ---------------- 이탈 분석 ----------------
def _num(s):
    s = str(s).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        m = re.match(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?$", s)     # 0:15 같은 시간
        if m:
            a, b, c = (int(x) if x else None for x in m.groups())
            return a * 3600 + b * 60 + c if c is not None else a * 60 + b
        return None


def read_retention(raw: bytes):
    """유튜브 스튜디오 시청 지속 CSV → [(위치 0~100%, 남은 시청자 %)]. 열 이름이 바뀌어도 숫자 열 2개로 읽어요."""
    text = raw.decode("utf-8-sig", "ignore")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(x.strip() for x in r)]
    if len(rows) < 3:
        raise RuntimeError("CSV에 숫자가 너무 적어요. 시청 지속 데이터를 내보낸 파일이 맞는지 확인해 주세요.")
    head = [h.strip().lower() for h in rows[0]]
    body = rows[1:]
    num_cols = [i for i in range(len(head)) if sum(_num(r[i]) is not None for r in body if i < len(r)) >= len(body) * 0.8]
    if len(num_cols) < 2:
        raise RuntimeError("CSV에서 '영상 위치'와 '시청 유지율' 숫자 열을 찾지 못했어요.")
    def pick(words, default):
        for i in num_cols:
            if any(w in head[i] for w in words):
                return i
        return default
    pos_i = pick(["position", "위치", "time", "시간"], num_cols[0])
    rest = [i for i in num_cols if i != pos_i]
    ret_i = pick(["absolute", "절대", "retention", "유지", "지속", "audience"], rest[0])
    if ret_i == pos_i:
        ret_i = rest[0]
    pts = [(_num(r[pos_i]), _num(r[ret_i])) for r in body if max(pos_i, ret_i) < len(r)]
    pts = [(p, v) for p, v in pts if p is not None and v is not None]
    pts.sort()
    if not pts:
        raise RuntimeError("CSV에서 숫자를 읽지 못했어요.")
    mx_p = pts[-1][0] or 1
    if mx_p <= 1.0001:                     # 0~1 비율이면 %로
        pts = [(p * 100, v) for p, v in pts]
    elif mx_p > 100:                       # 초 단위면 %로
        pts = [(p / mx_p * 100, v) for p, v in pts]
    if max(v for _, v in pts) <= 1.5:      # 유지율이 0~1이면 %로
        pts = [(p, v * 100) for p, v in pts]
    return pts


def retention_summary(pts, total_secs=None):
    """훅에서 새는지 / 급락 구간(절벽) 3곳 / 끝까지 남은 비율"""
    def at(pct):
        best = min(pts, key=lambda x: abs(x[0] - pct))
        return best[1]
    def to_t(pct):
        if not total_secs:
            return f"{pct:.0f}% 지점"
        s = total_secs * pct / 100
        return f"{int(s // 60)}:{int(s % 60):02d}"
    hook_pct = (3 / total_secs * 100) if total_secs else 5
    drops = []
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if p2 > hook_pct:          # 첫 몇 초는 따로 봄
            drops.append((v1 - v2, p1, p2, v1, v2))
    drops.sort(reverse=True)
    avg = (sum(max(0, x[0]) for x in drops) / len(drops)) if drops else 0
    floor = max(2.0, avg * 3)      # 평소보다 확 빠진 곳만 '절벽'으로 (서서히 빠지는 건 빼기)
    cliffs = []
    for d, p1, p2, v1, v2 in drops:
        if d < floor or len(cliffs) >= 3:
            break
        if all(abs(p1 - c["from_pct"]) > 4 for c in cliffs):
            cliffs.append({"from_pct": round(p1, 1), "to_pct": round(p2, 1), "time": to_t(p1),
                           "before": round(v1, 1), "after": round(v2, 1), "drop": round(d, 1)})
    cliffs.sort(key=lambda c: c["from_pct"])
    return {
        "start": round(pts[0][1], 1),
        "after_hook": round(at(hook_pct), 1), "hook_label": "첫 3초 뒤" if total_secs else "5% 지점",
        "half": round(at(50), 1), "end": round(pts[-1][1], 1),
        "cliffs": cliffs,
    }


def cut_at(data, pct):
    """완성 영상 길이 기록(durs)으로 그 지점이 몇 번 장면인지"""
    durs = data.get("durs") or []
    cuts = data.get("cuts") or []
    if not durs or len(durs) != len(cuts):
        return None
    total = sum(durs)
    t, target = 0.0, total * pct / 100
    for c, d in zip(cuts, durs):
        if t + d >= target:
            return c
        t += d
    return cuts[-1]


def retention_advice(cfg, summary, script_text=""):
    from vm import ai
    prompt = f"""유튜브 쇼츠/영상의 시청 지속 결과예요. 60대 사장님이 바로 고칠 수 있게 한국어로 짧게 조언해 주세요.

[숫자]
- 시작: {summary['start']}%  · {summary['hook_label']}: {summary['after_hook']}%  · 절반 지점: {summary['half']}%  · 끝: {summary['end']}%
- 급락 구간: {json.dumps(summary['cliffs'], ensure_ascii=False)}

[그 영상 대본 — 장면 번호와 그 장면이 나오는 시간]
{script_text or '(대본 없음 — 숫자만 보고 판단)'}

할 일:
1. 한 줄 판정: 훅(첫 3초)에서 새는지 / 중간 절벽이 문제인지 / 서서히 빠지는지.
2. 급락 구간마다: 그때 나오던 장면·대사(대본이 있으면)와 왜 나갔을지 짐작, 다음 영상에서 바꿀 점 한 줄.
3. 다음 영상에서 딱 하나만 고친다면 무엇인지.
숫자는 위에 있는 것만 쓰고 지어내지 말 것. 짐작은 짐작이라고 말할 것.
JSON만: {{"verdict":"","cliffs":[{{"time":"","scene":"","why":"","fix":""}}],"one_fix":""}}"""
    return ai._json_from(ai._claude(cfg, prompt, max_tokens=3000))
