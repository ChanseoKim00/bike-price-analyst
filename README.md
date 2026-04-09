# Bike Price Analyst

자전거 판매처 URL을 붙여넣으면 부품 개별 구매가 대비 얼마를 절약하는지 알려주는 분석 툴

**배포 URL**: https://bike-price-analyst-production.up.railway.app

---

## 주요 기능

- 자전거 판매 페이지 URL 입력 → HTML 스크래핑 → AI 자전거 정보 추출
- 구동계 / 휠셋 / 프레임셋 / 안장 / 핸들바 개별 공식 판매가 조회 (AI 웹 검색)
- 완성차 가격 vs 부품 합산가 비교 → 절약 금액 및 절약률 산출
- 부품 가격 DB 캐시 (TTL 기반 — 구동계 90일, 휠셋 60일, 프레임셋 120일 등)
- 에러별 구체적인 안내 메시지 및 다음 행동 유도

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 백엔드 | Flask (Python) |
| DB | PostgreSQL (Railway 호스팅) |
| AI | Anthropic Claude API (claude-sonnet-4-6) |
| 스크래핑 | requests + BeautifulSoup |
| 프론트엔드 | HTML / CSS / JS (Jinja2 템플릿) |
| 배포 | Railway |

---

## 요구사항 문서

[bike_price_analyst_requirements.md](./bike_price_analyst_requirements.md)

---

## 현재 한계점

### 부품 가격 정확도
AI 웹 검색 기반이라 판매처별 가격 편차가 존재한다. 특히 휠셋과 프레임셋은 정확도가 낮다.
주요 판매처별 스크래핑 모듈을 추가하면 개선 가능하다.

### Anthropic API Rate Limit
캐니언 등 HTML이 매우 큰 사이트는 분석 시 rate limit(429)에 걸릴 수 있다.
현재는 60초 대기 후 1회 재시도하며, 재시도도 실패하면 에러 페이지를 표시한다.

### 자바스크립트 렌더링 사이트 미지원
네이버 스마트스토어 등 JS로 렌더링되는 사이트는 requests로 HTML을 가져올 수 없어 지원하지 않는다.
Selenium 도입 시 해결 가능하다.

### 분석 중 백엔드 취소 불가
사용자가 로고를 클릭해 메인 페이지로 이동해도 백엔드(스크래핑 → AI 분석 → 부품 조회)는 계속 실행된다.
gunicorn 동기 워커는 클라이언트 연결 끊김을 감지하지 못한다.
Celery + Redis 도입 또는 gunicorn gevent 워커 전환으로 해결 가능하다.

### 프레임셋 가격 조회 불가
프레임셋은 특정 공식 대리점에서만 판매되는 경우가 많아 AI 웹 검색으로 가격을 찾기 어렵다.
현재는 AI 검색 대상에서 제외하고, 필요한 경우 DB에 수동으로 입력한다.

### 안장 / 핸들바 가격 미재시도
DB에 가격이 null로 저장된 안장과 핸들바는 재검색하지 않는다.
구동계와 휠셋만 재시도 대상이다.

### part_name_normalized 일관성
같은 부품이라도 판매 페이지 표기가 다르면 AI가 다른 normalized 이름을 생성해 중복 저장될 수 있다.
현재는 프롬프트로 규칙을 명시해 완화하고 있으나 완전한 해결은 아니다.

---

## 향후 추가 예정 기능

| 우선순위 | 기능 |
|----------|------|
| Nice | 회원가입 / 로그인 (Flask 자체 구현) |
| Nice | 분석 히스토리 (사용자별 과거 분석 결과 저장 및 조회) |
| Later | 가성비 티어리스트 (analyses DB 기반, 회원 전용 공개) |
| Later | Selenium 기반 JS 렌더링 사이트 지원 (네이버 스마트스토어 등) |
| Later | 부품별 주요 판매처 스크래핑 모듈 (가격 정확도 개선) |
| Later | 부품 가격 자동 갱신 워커 (TTL 만료 부품 백그라운드 재조회) |
| Later | 분석 취소 기능 (Celery + Redis 또는 gevent 워커 전환) |
| Later | 파워미터 포함 여부 표시 |
