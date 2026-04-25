# Bike Price Analyst

자전거 판매처 URL을 붙여넣으면 부품 개별 구매가 대비 얼마를 절약하는지 알려주는 분석 툴

**배포 URL**: https://bike-price-analyst-production.up.railway.app

---

## 주요 기능

### 자전거 분석
- 자전거 판매 페이지 URL 입력 → HTML 스크래핑 → AI 자전거 정보 추출
- requests 1차 시도 후 본문이 500자 미만이면 Playwright headless Chromium으로 폴백 (JS 렌더링 사이트 대응)
- 구동계 / 휠셋 / 안장 / 핸들바 개별 공식 판매가 조회 (Claude `web_search` 도구)
- 완성차 가격 vs 부품 합산가 비교 → 절약 금액 및 절약률 산출
- 프레임 소재(카본/알로이/스틸/티타늄), 브레이크 타입(유압 디스크/기계식 디스크/림) 자동 추출
- 외화(USD/EUR) 표기 가격은 한국은행 ECOS API 환율로 원화 변환
- 부품 가격 DB 캐시 (TTL: 구동계 90일 / 휠셋 60일 / 프레임셋 120일 / 안장·핸들바 180일)
- **Celery 비동기 처리** — 분석 요청 시 즉시 task_id 반환, `/analyze/status/<task_id>` 폴링으로 진행 상황 확인
- 분석 진행 중 `/analyze/cancel/<task_id>`로 취소 가능
- 에러별 구체적인 안내 메시지 및 다음 행동 유도

### 가격 변동 그래프 (World Tour 전용)
- 완성차 / 프레임셋 / 구동계 / 휠셋 최근 3년 가격 이력을 Chart.js로 시각화
- 가격 자동 갱신 워커가 "변동 없음" 확인 도장까지 남겨 그래프 연속성 확보

### 가격 자동 갱신 워커
- `worker/price_updater.py` — Railway Worker 서비스로 배포, 매주 월요일 03:00 KST 실행
- TTL 만료된 parts 전체 재조회 → 가격 변동 시 `part_price_history`에 기록
- `stale=True` 표시된 bikes는 `official_url` 재스크랩 → 가격 변동 시 `bike_price_history`에 기록

### 회원 / 플랜
- 이메일·비밀번호 기반 회원가입 / 로그인 (Flask 자체 구현, 비밀번호는 werkzeug 해시)
- **Google 소셜 로그인** 지원 (authlib OIDC) — 같은 이메일 계정이 있으면 자동 연결, 없으면 신규 가입 (닉네임 자동 생성)
- 비밀번호 재설정 (`/forgot-password`, 30분 일회용 토큰, SendGrid 이메일)
- 가입 시 이름 / 닉네임 / 생년월일 / 개인정보 동의 필수 (로컬 가입 기준)
- 로그인 시 분석 히스토리 자동 저장, `/history`에서 본인 과거 분석 결과 조회
- 플랜 3종: `continental` / `pro` / `world_tour` (가입 기본값 = continental)
- 플랜별 분석 횟수 및 상세 정보 열람 제한 (아래 "플랜 구조" 참조)

### 마이페이지
- `/mypage` — 닉네임, 이름, 생년월일 등 프로필 정보 조회
- 최근 분석 히스토리 목록 확인 및 키워드 검색
- 현재 가입 플랜 및 요금제 안내 링크 제공
- 이메일 알림 수신 여부 설정

### 부품 가격 제안
- 분석 결과 화면에서 사용자가 부품별 더 저렴한 가격과 판매처 URL 제안 (`/suggest`)
- 제안은 `pending` 상태로 저장되며 관리자가 `/admin/suggestion/<id>`에서 검토·반영·반려

### 유저 피드백
- `/feedback` — 만족도(1–10점) + 불편한 점 / 좋은 점 / 개발자에게 하고 싶은 말 입력 (각 2000자 이내)
- 분석 결과 페이지 이탈 시 이탈 팝업 — 만족도 점수만 빠르게 받는 간단 피드백 (`/feedback/quick` POST)
- 로그인 유저는 72시간 내 피드백을 완료했으면 팝업 미표시 (게스트는 브라우저 localStorage로 쿨타임 관리)
- 비로그인 제출 가능 (user_id nullable)

### AI 상담원 (챗봇)
- `/chatbot` — Claude Sonnet 4.6 기반 앱 사용법·요금제·로드맵 안내 챗봇
- 시스템 프롬프트에 README / ROADMAP을 컨텍스트로 주입 + prompt caching 적용
- 자주 묻는 질문 자동응답 버튼 (`auto_message.json`) — 채팅창 내 원클릭 선택 가능
- 방문자별 하루 30개 메시지 제한 (`chatbot_usage_logs`, admin은 무제한)

### 관리자 기능
- `role=admin`인 계정만 `/admin` 접근 가능
- 전체 사용자 수 / 분석 수 / 최근 분석 / 사용자 목록 조회
- 대기 중인 가격 제안(`PriceSuggestion`) 검토 및 반영·반려
- 유저 피드백(`UserFeedback`) 목록 조회 및 상세 내용 확인 (`/admin/feedback/<id>`)
- admin은 모든 플랜 기능(가격 그래프 포함) 및 챗봇 사용 제한 없음

---

## 플랜 구조

| 플랜 | 가격 | 분석 횟수 | 제공 정보 |
|------|------|-----------|-----------|
| **비로그인** (게스트) | — | IP당 5시간 내 3회 | 총 절약 금액만 표시 (부품가 블러) |
| **Continental** (가입 기본) | 무료 (광고 포함) | 5시간 내 상세 분석 10회 | 총 절약 금액 + 개별 부품 가격 |
| **Pro** | 유료 | 무제한 | Continental + 광고 제거 |
| **World Tour** | 유료 (풀옵션) | 무제한 | Pro + 부품·완성차 가격 변동 그래프 + 가격 알림 *(예정)* |

> 횟수 초과 시: 비로그인은 분석 자체 차단, Continental은 분석은 되지만 부품가가 블러 처리된다. `AnalysisLog` 테이블로 5시간 롤링 윈도우 기준 카운트한다.

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 백엔드 | Flask (Python) |
| DB | PostgreSQL (Railway 호스팅) |
| ORM | SQLAlchemy |
| 인증 | Flask session + werkzeug password hash |
| AI | Anthropic Claude API (claude-sonnet-4-6, `web_search` 도구) |
| 스크래핑 | requests + BeautifulSoup + Playwright (JS 렌더링 폴백) |
| 비동기 | Celery + Redis (분석 작업 큐, 취소 지원) |
| 환율 | 한국은행 ECOS API |
| 이메일 | SendGrid (비밀번호 재설정) |
| 프론트엔드 | HTML / CSS / JS (Jinja2), Chart.js (가격 그래프) |
| 배포 | Railway (웹 + Worker 2-서비스) |

---

## 주요 경로

| 경로 | 설명 |
|------|------|
| `/` | 메인 (URL 입력) |
| `/analyze` | 분석 실행 (POST) |
| `/analyze/status/<task_id>` | 분석 진행 상태 폴링 |
| `/analyze/cancel/<task_id>` | 분석 작업 취소 |
| `/pricing` | 요금제 안내 |
| `/register`, `/login`, `/logout` | 인증 |
| `/forgot-password`, `/reset-password/<token>` | 비밀번호 재설정 |
| `/auth/google/login`, `/auth/google/callback` | Google OAuth |
| `/mypage` | 마이페이지 (로그인 필요) |
| `/history` | 본인 분석 히스토리 (로그인 필요) |
| `/suggest` | 부품 가격 제안 |
| `/feedback` | 유저 피드백 폼 |
| `/feedback/complete` | 피드백 제출 완료 |
| `/feedback/quick` | 이탈 팝업 빠른 피드백 (POST) |
| `/chatbot` | AI 상담원 챗봇 |
| `/admin` | 관리자 대시보드 (admin 전용) |
| `/admin/suggestion/<id>` | 가격 제안 상세 · 승인 / 반려 |
| `/admin/feedback/<id>` | 유저 피드백 상세 조회 (admin 전용) |

---

## 환경변수

| 변수 | 용도 |
|------|------|
| `DATABASE_URL` | PostgreSQL 접속 문자열 |
| `FLASK_SECRET_KEY` | Flask 세션 암호화 키 (미설정 시 부팅 시 랜덤) |
| `ANTHROPIC_API_KEY` | Claude API (자전거 분석 · 부품 검색 · 챗봇) |
| `BOK_API_KEY` | 한국은행 ECOS 환율 API (미설정 시 폴백 환율 사용) |
| `SENDGRID_API_KEY` | SendGrid 이메일 발송 (비밀번호 재설정 메일) |
| `SENDGRID_FROM_EMAIL` | SendGrid Verified Sender 주소 |
| `GOOGLE_CLIENT_ID` | Google OAuth Client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth Client Secret |

### Google OAuth 클라이언트 만들기
1. [Google Cloud Console](https://console.cloud.google.com/) → 프로젝트 선택/생성
2. **APIs & Services → OAuth consent screen** 에서 앱 등록 (User Type: External, scope: email·profile·openid)
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID** (Type: Web application)
4. **Authorized redirect URIs** 에 로컬 / 배포 주소를 모두 등록:
   - `http://localhost:8080/auth/google/callback`
   - `https://bike-price-analyst-production.up.railway.app/auth/google/callback`
5. 생성된 Client ID / Secret을 환경변수에 등록

---

## 요구사항 문서

[bike_price_analyst_requirements.md](./bike_price_analyst_requirements.md)

로드맵은 [ROADMAP.md](./ROADMAP.md) 참고.

---

## 현재 한계점

### 부품 가격 정확도
AI 웹 검색 기반이라 판매처별 가격 편차가 존재한다. 특히 휠셋과 프레임셋은 정확도가 낮다.
사용자의 가격 제안(`/suggest`)과 워커의 주기 재조회로 보완한다.

### Anthropic API Rate Limit
캐니언 등 HTML이 매우 큰 사이트는 분석 시 rate limit(429/529)에 걸릴 수 있다.
현재는 60초 대기 후 1회 재시도하며, 재시도도 실패하면 "서비스 혼잡" 에러 페이지를 표시한다.

### 로그인 필요 사이트 미지원
로그인 후 AJAX로 데이터를 불러오는 사이트는 Playwright 폴백으로도 스크래핑이 어렵다. 현재 구조상 해결 어려움.

### 프레임셋 가격 조회 불가
프레임셋은 공식 대리점에서만 판매되는 경우가 많아 AI 웹 검색에서 제외한다.
`parts` 테이블에 `price_krw=null`로만 저장되며, 필요한 경우 관리자가 가격 제안 승인 등으로 수동 입력한다.

### 안장 / 핸들바 가격 미재시도
DB에 가격이 null로 저장된 안장·핸들바는 재검색하지 않는다 (`RETRY_ON_NULL_TYPES`에 구동계·휠셋만 포함).

### part_name_normalized 일관성
같은 부품이라도 판매 페이지 표기가 다르면 AI가 다른 normalized 이름을 생성해 중복 저장될 수 있다.
프롬프트 규칙 + `_normalize_part_name` 후처리 + 접두어 매칭(`LIKE prefix%`)으로 완화 중이나 완전한 해결은 아니다.

### 결제 미연동
`pro` / `world_tour` 플랜은 DB 컬럼과 접근 제어만 구현되어 있고, 결제(Stripe)는 아직 연동 전이다.
현재는 관리자가 수동으로 `users.plan`을 변경해 부여한다.

---

## 향후 추가 예정 기능

| 우선순위 | 기능 |
|----------|------|
| Next | Stripe 결제 연동 (Pro / World Tour 구독) |
| Next | 가격 알림 (목표 가격 이하 도달 시 이메일) |
| Later | 가성비 티어리스트 (analyses DB 기반, 회원 전용 공개) |
| Later | 부품별 주요 판매처 스크래핑 모듈 (가격 정확도 개선) |
| Later | 파워미터 포함 여부 표시 |
