"""플로우가 거부하기 쉬운 격한 단어를 부드러운 말로 바꾸기 (대사·장면 묘사용).
나레이션(목소리)은 플로우를 거치지 않으므로 바꾸지 않습니다."""
import re

KO = [
    (r"(?<![가-힣])돈(?=[을이은도만에,\s.!?…\"']|$)", "봉투"),
    (r"현금", "봉투"), (r"보험금", "보장금"), (r"통장", "저금 수첩"), (r"빚", "갚을 것"),
    (r"내놔(요)?", "주세요"), (r"내놓(아|으)", "건네"), (r"뺏(어|었|겠)", "가져가"), (r"훔(쳐|친|쳤)", "몰래 가져간"),
    (r"도둑", "몰래 가져간 사람"), (r"사기꾼", "거짓말쟁이"), (r"사기", "속임수"),
    (r"병원", "건강센터"), (r"응급실", "건강센터"), (r"진료", "검사"), (r"수술", "치료"), (r"입원", "쉬러 간"),
    (r"의사", "선생님"), (r"환자", "어르신"), (r"주사", "치료"), (r"피(?=[가를는\s.!?]|$)", "상처"),
    (r"죽(어|었|을|는|겠)", "떠나"), (r"죽음", "이별"), (r"시체", "누운 모습"), (r"자살", "힘든 선택"),
    (r"때리", "밀치"), (r"때려", "밀쳐"), (r"맞았", "부딪혔"), (r"칼", "주방 도구"), (r"총", "물건"),
    (r"경찰", "동네 어른"), (r"신고", "알리기"), (r"감옥", "먼 곳"), (r"술(?=[을이은에\s.!?]|$)", "음료"),
    (r"담배", "버릇"), (r"미친", "정신없는"), (r"죽여", "혼내"), (r"꺼져", "나가 주세요"), (r"닥쳐", "그만해요"),
]
EN = [
    (r"\bmoney\b", "envelope"), (r"\bcash\b", "envelope"), (r"\binsurance payout\b", "family document"),
    (r"\bhospital\b", "health center"), (r"\bER\b", "health center"), (r"\bemergency room\b", "health center"),
    (r"\bmedical (exam|examination|consultation|treatment)\b", "checkup"), (r"\bsurgery\b", "care"),
    (r"\bdoctor\b", "counselor in a white coat"), (r"\bpatient\b", "elderly person"), (r"\bblood\b", "red mark"),
    (r"\bdead\b", "gone"), (r"\bdie(s|d)?\b", "pass away"), (r"\bcorpse\b", "lying figure"),
    (r"\bhit(s|ting)?\b", "push"), (r"\bslap(s|ped|ping)?\b", "push"), (r"\bknife\b", "kitchen tool"),
    (r"\bgun\b", "object"), (r"\bpolice\b", "neighbor"), (r"\bsteal(s|ing)?\b", "take secretly"),
    (r"\bstole(n)?\b", "took secretly"), (r"\bthief\b", "someone"), (r"\bdrunk\b", "tired"),
    # 미성년자로 보이는 말 → 성인 (플로우는 아이·청소년 영상을 거부함)
    (r"\bteen(age|aged)?\s+girl\b", "young woman in her twenties"), (r"\bteen(age|aged)?\s+boy\b", "young man in his twenties"),
    (r"\bteen(ager)?s?\b", "young adult in their twenties"), (r"\b(little|young)\s+girl\b", "young woman in her twenties"),
    (r"\b(little|young)\s+boy\b", "young man in his twenties"), (r"\bschool\s?girl\b", "young woman in her twenties"),
    (r"\bschool\s?boy\b", "young man in his twenties"), (r"\b(child|kid)(ren|s)?\b", "adult son or daughter in their twenties"),
    (r"\bhigh school student\b", "university student in her twenties"), (r"\bmiddle school student\b", "university student in her twenties"),
    (r"\b(a|the)\s+girl\b", r"\1 young woman"), (r"\b(a|the)\s+boy\b", r"\1 young man"),
    (r"\bponytail with (a )?(red )?ribbon\b", "neat ponytail"), (r"\bschool uniform\b", "neat casual outfit"),
    (r"\bcigarette(s)?\b", "habit"), (r"\bangry\b", "upset"), (r"\bangrily\b", "with a stern face"), (r"\bfurious\b", "upset"), (r"\bscream(s|ing)?\b", "call out"),
]


def soften(text):
    if not text:
        return text
    t = text
    for pat, rep in KO:
        t = re.sub(pat, rep, t)
    for pat, rep in EN:
        t = re.sub(pat, rep, t, flags=re.I)
    return t
