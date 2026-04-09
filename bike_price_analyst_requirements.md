# Bike Price Analyst — 프로젝트 요구사항 문서

---

## 1. 프로젝트 개요

- **앱 이름**: Bike Price Analyst
- **한 줄 설명**: 자전거 판매처 URL을 붙여넣으면 부품 개별 구매가 대비 얼마를 절약하는지 알려주는 분석 툴
- **해결하려는 문제**:
  1. 여러 자전거 사이에서 어떤 모델이 금전적으로 가장 이득인지 파악하기 어려움
  2. 부품 개별가를 일일이 찾아서 직접 계산해야 하는 과정이 매우 번거로움

---

## 2. 대상 사용자

### 시나리오 1: 김바보 (21세 대학생, 입문자)

생일 앞두고 입문용 로드 바이크를 찾는 중.

1. 앱을 열고 자바 실룰로 6의 판매처 URL을 붙여넣는다
2. 분석 버튼을 누른다
3. 프레임은 따로 판매하지 않아 프레임 제외 부품 가격이 출력된다
4. LTWOO A9 구동계는 정규화 목록에 없지만 열린 목록 방식으로 "ltwoo_a9"로 저장되어 정상 분석된다
5. 프레임을 약 5만원에 사는 셈이라는 결과를 보고 구매를 결정한다

> **발견된 요구사항**: 정규화 목록을 닫힌 목록이 아닌 열린 목록으로 운영. 구동계 자체가 파악 불가한 경우에만 "구동계 확인 불가" 메시지 출력

### 시나리오 2: 이중간 (30세 직장인, 중급자)

리파인드 옥타와 판타시아 Radar 9 중에 고민 중.

1. 리파인드 옥타 URL을 붙여넣고 분석한다
2. 프레임 미판매 모델이라 프레임 80만원 값어치로 구매 가능하다는 결과 확인
3. 판타시아 Radar 9 URL을 붙여넣고 분석한다
4. 부품 개별 구매 대비 220만원 절약 가능하다는 결과 확인
5. 겉으로 비슷해 보였지만 판타시아가 훨씬 가성비가 좋다는 사실을 알게 됨

> **발견된 요구사항**: 여러 자전거를 연속 분석하고 비교할 수 있어야 함

### 시나리오 3: 박천재 (40세 전문직, 고급자)

스페셜라이즈드 타막 SL8 S-Works의 2025년식 vs 2026년식 중 고민 중.

1. 2026년식 URL을 붙여넣고 분석 → 부품 대비 350만원 절약
2. 2025년식 URL을 붙여넣고 분석 → 부품 대비 500만원 절약
3. 연식 차이로 바뀐 게 거의 없다는 걸 아는 고급자로서 150만원 더 저렴한 2025년식 구매 결정

> **발견된 요구사항**: 같은 모델의 연식별 비교가 가능해야 함 (model_year 필드)

---

## 3. 기술 스택

| 영역 | 기술 | 비고 |
|------|------|------|
| 백엔드 | Flask (Python) | 기존 학습 경험 활용 |
| DB | PostgreSQL | Railway 호스팅 |
| AI | Anthropic Claude API | HTML 분석 + 부품 추출/정규화 |
| 스크래핑 | requests + BeautifulSoup | MVP에서 부품 가격은 AI 웹 검색으로 대체 가능 |
| 프론트엔드 | HTML / CSS / JS | Flask 템플릿 (Jinja2) |
| 배포 | Railway | 기존 배포 경험 활용 |

---

## 4. 핵심 기능 (MVP)

| # | 기능 | 설명 |
|---|------|------|
| 1 | URL 입력 + HTML 스크래핑 | 판매처 URL을 붙여넣으면 HTML을 가져온다 |
| 2 | AI 자전거 정보 추출 | HTML에서 브랜드, 모델명, 연식, 가격, 부품 정보를 추출한다 |
| 3 | 부품별 가격 조회 | DB에 있으면 조회, 없으면 AI 웹 검색으로 가격을 찾아 DB에 저장 |
| 4 | 가격 비교 계산 | 완성차 가격 vs 부품 합산 가격을 비교하여 절약 금액 산출 |
| 5 | 결과 화면 표시 | 부품별 개별가, 합산가, 절약 금액, missing_parts 표시 |
| 6 | 에러 처리 | 스크래핑/분석 실패 시 에러 메시지 + 재시도 버튼 |

---

## 5. 추가 기능 (향후)

| 우선순위 | 기능 | 설명 |
|----------|------|------|
| Nice | 회원가입/로그인 | Supabase Auth 또는 Flask 자체 구현 |
| Nice | 분석 히스토리 | 사용자별 과거 분석 결과 저장/조회 |
| Later | 가성비 티어리스트 | analyses DB 기반, 회원 전용 공개 |
| Later | 부품 가격 자동 업데이트 | ttl_days 기반 가격 갱신 워커 |
| Later | 광고 | 수입사/판매처 광고 영역 |
| Later | Selenium 기반 JS 렌더링 사이트 지원 | 네이버 스마트스토어 등 동적 렌더링 쇼핑몰 스크래핑 |

---

## 6. DB 스키마

### 테이블 1: parts (부품 DB)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | uuid, PK | |
| part_type | text | groupset / wheelset / frameset / saddle / handlebar |
| part_name | text | 원본 표기 (예: "Shimano Dura-Ace Di2 R9200") |
| part_name_normalized | text | 정규화명 (예: "shimano_dura_ace_di2") — 열린 목록 |
| price_krw | integer | 공식 판매가 (원) |
| official_url | text | 가격 출처 URL |
| last_verified_at | timestamp | 마지막 가격 확인일 |
| last_checked_at | timestamp | 마지막 조회일 (워커용) |
| ttl_days | integer | 가격 유효 기간 (groupset=90, wheelset=60, frameset=120, saddle=180, handlebar=180) |
| created_at | timestamp | |

### 테이블 2: bikes (완성차 DB)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | uuid, PK | |
| brand | text | 영문 소문자 (예: "specialized") |
| model_name | text | 예: "Tarmac SL8 S-Works" |
| model_year | integer | |
| price_krw | integer | 완성차 판매가 (원) |
| official_url | text | 판매처 URL |
| frame_material | text | carbon / alloy / steel / titanium / other / unknown |
| frame_material_confidence | float | 0~1 |
| frame_material_source | text | page_text / model_knowledge / unknown |
| brake_type | text | hydraulic_disc / mechanical_disc / rim / unknown |
| groupset_id | uuid, FK → parts | NOT NULL |
| wheelset_id | uuid, FK → parts | null 허용 |
| saddle_id | uuid, FK → parts | null 허용 |
| weight_kg | float | null 허용 |
| last_verified_at | timestamp | |
| stale | boolean | 기본값 false, 부품가 변동 감지 시 true |
| created_at | timestamp | |

- **(brand, model_name, model_year) UNIQUE 제약**

### 테이블 3: analyses (분석 결과 캐시)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | uuid, PK | |
| bike_id | uuid, FK → bikes | |
| parts_sum_krw | integer | 부품 합산 공식가 |
| saving_krw | integer | parts_sum - 완성차가 (음수 허용) |
| saving_pct | float | 절약률 |
| missing_parts | text[] | 비교 제외 부품 (예: ["saddle", "wheelset"]) |
| analyzed_at | timestamp | |

### 인덱스

- parts: last_checked_at 기준 인덱스 (가격 갱신 워커용)
- parts: part_name_normalized 기준 인덱스 (중복 체크용)

### 향후 추가 예정

- users 테이블: 회원가입/로그인 구현 시 추가 (Flask 자체 구현 또는 별도 auth 서비스)

---

## 7. 처리 흐름

### 사용자가 URL을 입력했을 때 전체 처리 흐름

**STEP 1 — 스크래핑**
입력받은 URL에 접속해서 HTML을 가져온다.
실패 시 (링크 만료, 봇 차단, 네트워크 오류) → 케이스 6 처리.

**STEP 2 — AI 분석**
HTML을 AI에게 넘겨서 아래 정보를 추출하고 정규화한다.
- 추출 항목: brand, model_name, model_year, price_krw, frame_material, brake_type, groupset, wheelset, frameset, saddle, handlebar (각각 원본명 + 정규화명)
- brand, model_name, groupset 중 하나라도 추출 불가 시 → 케이스 6 처리

**STEP 3 — bikes 테이블 확인**
(brand + model_name + model_year) 조합으로 bikes 테이블 조회.
- 있으면 → STEP 4로
- 없으면 → bikes 신규 행 생성 후 STEP 4로

**STEP 4 — parts 테이블 확인 및 처리**
각 부품의 part_name_normalized로 parts 테이블 조회.
- 있으면 → 해당 part_id 사용
- 없으면 → AI 웹 검색으로 가격 조회 후 parts 신규 행 생성 (MVP)
- 명시 안 된 부품 → null 처리, missing_parts에 추가

**STEP 5 — 케이스 분류 및 결과 출력**

| 케이스 | bikes | parts | 처리 |
|--------|-------|-------|------|
| 1 | ✓ 있음 | ✓ 전부 있음 | 즉시 분석 결과 출력 |
| 2 | ✗ 없음 | ✓ 전부 있음 | bikes 생성 → 분석 결과 출력 |
| 3 | ✗ 없음 | △ 일부 없음 | bikes 생성 → 없는 부품 조회 → 분석 결과 출력 |
| 4 | ✗ 없음 | ✗ 전부 없음 | bikes 생성 → 모든 부품 조회 → 분석 결과 출력 |
| 5 | ✓ 있음 | △ 일부 없음 | 없는 부품만 조회 → 분석 결과 출력 |
| 6 | — | — | 스크래핑/분석 실패 → 에러 메시지 + 재시도 버튼 |

---

## 8. 개발 순서

| Step | 내용 | 비고 |
|------|------|------|
| 1 | DB 스키마 생성 | PostgreSQL 테이블 + 인덱스 + 제약조건 |
| 2 | 백엔드 핵심 로직 | URL → 스크래핑 → AI 분석 → DB 저장 → 결과 반환 |
| 3 | 프론트엔드 기본 UI | URL 입력창 + 분석 버튼 + 결과 표시 화면 |
| 4 | 백엔드 + 프론트엔드 연결 | 화면에서 URL 입력 → 실제 분석 결과 표시 |
| 5 | 에러 처리 | 6개 케이스별 대응 + 사용자 안내 메시지 |
| 6 | Railway 배포 | PostgreSQL 추가 + 환경변수 설정 |
| 7 | UI 다듬기 + 테스트 | 다양한 자전거 URL로 테스트 |
| 8 (향후) | 로그인 + 티어리스트 | 데이터 충분히 쌓인 후 |

---

---

## 9. 현재 한계점 (향후 개선 필요)

- 부품 가격 정확도가 낮음 (특히 휠셋, 프레임셋)
- 원인: AI 웹 검색 기반이라 판매처별 가격 편차가 큼
- 개선 방향: 주요 판매처별 스크래핑 모듈 추가
- 에러페이지 떳을 때 에러 사유 알려주면 UX 크게 개선

### 분석 중 취소 기능 (백엔드 중단) 미지원

- **현상**: 로고 클릭 시 프론트엔드는 즉시 메인 페이지로 이동하지만, 백엔드(스크래핑 → AI 분석 → 부품 조회)는 계속 실행됨
- **원인**: gunicorn 동기 워커는 클라이언트 연결 끊김을 감지하지 못함. 요청이 시작되면 중간에 멈출 지점이 없음
- **완화책**: 분석 결과가 DB에 캐시되므로 같은 URL 재분석 시 즉시 결과 반환 (한 번만 기다리면 됨)
- **개선 방향**:
  - Celery + Redis 도입: 작업을 백그라운드 큐에 넣고 폴링으로 상태 확인 및 취소 가능 (난이도 높음)
  - gunicorn gevent 워커로 교체: 비동기 워커로 클라이언트 연결 끊김 감지 가능 (난이도 중간)
- **우선순위**: 사용자 규모가 커진 후 도입 검토

---

*문서 버전: v1.0 — 9주차 Day 5 작성*
*기술 스택 결정: Flask + PostgreSQL + Railway (기존 학습 경험 기반)*
