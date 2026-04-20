"""
랜덤 닉네임 생성 유틸 — 소셜 로그인 신규 가입 시 자동 닉네임 부여용.

형식: 형용사 + 동물 + 3자리 숫자 (예: "맑은기린122", "청소원숭이356")
중복이면 숫자 재추첨, 일정 횟수 실패 시 4자리 숫자로 확장.
"""
import random

from ..models import User, db


_ADJECTIVES = [
    "맑은", "푸른", "빠른", "조용한", "용감한", "지혜로운", "다정한", "쾌활한",
    "느긋한", "포근한", "산뜻한", "씩씩한", "늠름한", "싱그러운", "따뜻한", "상큼한",
    "귀여운", "도도한", "날렵한", "온화한", "환한", "해맑은", "정겨운", "찬란한",
    "반짝이는", "평온한", "활기찬", "친절한", "든든한", "기운찬",
]

_ANIMALS = [
    "기린", "원숭이", "고양이", "호랑이", "사자", "수달", "판다", "여우",
    "토끼", "다람쥐", "너구리", "코끼리", "곰", "늑대", "사슴", "부엉이",
    "독수리", "펭귄", "돌고래", "거북이", "고래", "햄스터", "앵무새", "족제비",
    "고슴도치", "미어캣", "카피바라", "알파카", "재규어", "두더지",
]

_MAX_RETRIES_3DIGIT = 10
_MAX_RETRIES_4DIGIT = 20


def generate_random_nickname(digits: int = 3) -> str:
    """형용사 + 동물 + N자리 숫자."""
    low = 10 ** (digits - 1)
    high = 10 ** digits - 1
    return f"{random.choice(_ADJECTIVES)}{random.choice(_ANIMALS)}{random.randint(low, high)}"


def generate_unique_nickname() -> str:
    """
    users.nickname UNIQUE에 걸리지 않는 닉네임을 찾아 반환.
    3자리로 10회 시도 → 실패 시 4자리로 20회 시도 → 그래도 실패 시 예외.
    """
    for _ in range(_MAX_RETRIES_3DIGIT):
        nickname = generate_random_nickname(digits=3)
        if not User.query.filter_by(nickname=nickname).first():
            return nickname

    for _ in range(_MAX_RETRIES_4DIGIT):
        nickname = generate_random_nickname(digits=4)
        if not User.query.filter_by(nickname=nickname).first():
            return nickname

    raise RuntimeError("닉네임 생성 재시도 한도 초과")
