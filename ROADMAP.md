# Bike Price Analyst — 로드맵

## 서비스 개요

자전거 판매 페이지 URL을 입력하면 부품 개별 구매가 대비 얼마를 절약하는지 알려주는 분석 툴.
트래픽을 모아 광고 수익과 구독 수익을 병행하는 SaaS 모델로 운영한다.

---

## 플랜 구조

| 플랜 | 가격 | 제공 기능 |
|------|------|-----------|
| **Continental** | 무료 (가입 기본) | 총 절약 금액 + 개별 부품 가격 (5시간 내 10회) / 광고 있음 |
| **Pro** | 유료 | Continental 무제한 + 광고 제거 |
| **World Tour** | 유료 (풀옵션) | Pro 모든 기능 + 부품·완성차 가격 변동 그래프 + 가격 알림 |

비로그인 게스트는 IP당 5시간 내 3회 분석 가능(부품가 블러).

---

## 수익 모델

- **초기**: 무료 트래픽 확보 + 광고 수익으로 API 비용 충당
- **중기**: 구독제(Pro / World Tour) 도입으로 안정적 수익 확보
- **장기**: 자전거 수입사 / 공식 대리점 B2B 협업 (할인 이벤트, 공식 가격 등록)

---

## 개발 로드맵

### ✅ Phase 1 — JS 렌더링 사이트 지원 (완료)
- `app/scraper.py` — requests 본문이 500자 미만이면 Playwright headless Chromium으로 폴백
- Railway 배포에 Playwright Chromium + 의존 라이브러리 포함 (`nixpacks.toml`, `railway.toml`)

### ⏳ Phase 2 — AI 정보 추출 정확도 개선 (진행 중)
- `ai_analyzer.py` 시스템 프롬프트 지속 강화 (브랜드별 normalized 규칙, 튜블리스 표기 제외 등)
- `_normalize_part_name` 후처리로 하이픈·공백·TLR 등 표기차 흡수
- AI가 추출한 normalized가 DB값의 접두어·역접두어일 때도 매칭하는 `LIKE prefix%` 조회 추가
- model_year 미추출 시 재시도 로직 추가

### ✅ Phase 3 — 플랜 구조 구현 (완료)
- `users.plan` 컬럼 추가 (continental / pro / world_tour, 기본 continental)
- 5시간 롤링 윈도우 기반 분석 횟수 카운트 (`analysis_logs`)
- 플랜별 분석 결과 화면 블러 처리 (게스트 / Continental 초과)

### Phase 4 — Stripe 결제 연동
- Stripe Checkout 연동
- Webhook 처리 (결제 완료 → `users.plan` 업데이트)
- 구독 만료 처리 (`plan_expires_at` 컬럼 추가)

### ✅ Phase 5 — 가격 자동 갱신 워커 + 가격 이력 저장 (완료)
- `worker/price_updater.py` — Railway Worker 서비스로 분리 배포 (`railway.worker.toml`)
- 매주 월요일 03:00 KST 실행 — TTL 만료 parts 재조회, `stale=True` bikes 재스크랩
- `part_price_history` / `bike_price_history` 테이블로 가격 이력 축적
- World Tour 플랜 전용 Chart.js 그래프 (완성차 / 프레임셋 / 구동계 / 휠셋, 최근 3년)

### Phase 6 — 가격 알림
- 알림 설정 UI (자전거별 목표 가격 입력)
- 가격 체크는 Phase 5 워커에 훅으로 연동
- 이메일 발송 (SendGrid 또는 SMTP)

### Phase 7 — AI 상담원 개선 (부가)
- 현재 `/chatbot` 에서 Claude Sonnet 4.6 + README/ROADMAP 컨텍스트 + prompt caching 운영 중
- 추후: 대화 이력 유지, 자주 묻는 질문 프리셋, 관리자 피드백 수집

---

## 고려 중인 기능

| 기능 | 상태 | 비고 |
|------|------|------|
| 가성비 티어리스트 | 검토 중 | 개인별 기준 차이 문제 — 기준 정립 후 결정 |
| 아이디 / 비밀번호 찾기 | 예정 | 이메일 발송 기능 구현 후 추가 |
| 분석 취소 기능 | 예정 | Celery + Redis 또는 gevent 워커 전환 필요 |
| 파워미터 포함 여부 표시 | 검토 중 | AI 추출 정확도 확보 후 |

---

## 현재 알려진 한계점

| 한계 | 원인 | 해결 계획 |
|------|------|-----------|
| 가격 정확도 편차 | AI 웹 검색 기반 | 사용자 가격 제안 + Phase 5 워커 주기 갱신으로 보완 |
| 로그인 필요 사이트 미지원 | 세션·AJAX 기반 사이트는 Playwright로도 어려움 | 구조상 보류 |
| 프레임셋 가격 조회 불가 | 공식 대리점 한정 판매 | AI 검색 제외, 가격 제안 승인으로 수동 입력 |
| part_name_normalized 중복 | AI 출력 불일치 | 프롬프트 강화 + 정규화 후처리 + 접두어 매칭으로 완화 중 |
| 분석 중 백엔드 취소 불가 | gunicorn 동기 워커 한계 | Celery + Redis 도입 후 해결 |
| Stripe 미연동 | 구현 전 | Phase 4 |

---

*마지막 업데이트: 2026-04-20*
