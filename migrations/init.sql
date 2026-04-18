-- Bike Price Analyst — DB 스키마
-- PostgreSQL (Railway)

-- UUID 생성 확장
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- Table 1: parts (부품 DB)
-- ============================================================
CREATE TABLE IF NOT EXISTS parts (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    part_type            TEXT        NOT NULL CHECK (part_type IN ('groupset', 'wheelset', 'frameset', 'saddle', 'handlebar')),
    part_name            TEXT        NOT NULL,
    part_name_normalized TEXT        NOT NULL,
    price_krw            INTEGER,
    official_url         TEXT,
    last_verified_at     TIMESTAMP,
    last_checked_at      TIMESTAMP,
    ttl_days             INTEGER     NOT NULL DEFAULT 90,
    created_at           TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- 인덱스: 가격 갱신 워커용
CREATE INDEX IF NOT EXISTS idx_parts_last_checked_at      ON parts (last_checked_at);
-- 인덱스: 중복 체크용
CREATE INDEX IF NOT EXISTS idx_parts_part_name_normalized ON parts (part_name_normalized);

-- ============================================================
-- Table 2: bikes (완성차 DB)
-- ============================================================
CREATE TABLE IF NOT EXISTS bikes (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    brand                       TEXT        NOT NULL,
    model_name                  TEXT        NOT NULL,
    model_year                  INTEGER     NOT NULL,
    price_krw                   INTEGER,
    official_url                TEXT,
    frame_material              TEXT        NOT NULL DEFAULT 'unknown'
                                    CHECK (frame_material IN ('carbon', 'alloy', 'steel', 'titanium', 'other', 'unknown')),
    frame_material_confidence   FLOAT       NOT NULL DEFAULT 0,
    frame_material_source       TEXT        NOT NULL DEFAULT 'unknown'
                                    CHECK (frame_material_source IN ('page_text', 'model_knowledge', 'unknown')),
    brake_type                  TEXT        NOT NULL DEFAULT 'unknown'
                                    CHECK (brake_type IN ('hydraulic_disc', 'mechanical_disc', 'rim', 'unknown')),
    groupset_id                 UUID        NOT NULL REFERENCES parts (id),
    wheelset_id                 UUID        REFERENCES parts (id),
    saddle_id                   UUID        REFERENCES parts (id),
    weight_kg                   FLOAT,
    last_verified_at            TIMESTAMP,
    stale                       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMP   NOT NULL DEFAULT NOW(),

    UNIQUE (brand, model_name, model_year)
);

-- ============================================================
-- Table 3: analyses (분석 결과 캐시)
-- ============================================================
CREATE TABLE IF NOT EXISTS analyses (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    bike_id         UUID        NOT NULL REFERENCES bikes (id),
    parts_sum_krw   INTEGER     NOT NULL,
    saving_krw      INTEGER     NOT NULL,
    saving_pct      FLOAT       NOT NULL,
    missing_parts   TEXT[]      NOT NULL DEFAULT '{}',
    analyzed_at     TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Table 4: users (회원)
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email               TEXT        NOT NULL UNIQUE,
    password_hash       TEXT        NOT NULL,
    role                TEXT        NOT NULL DEFAULT 'user'
                            CHECK (role IN ('user', 'admin')),
    created_at          TIMESTAMP   NOT NULL DEFAULT NOW(),
    last_login_at       TIMESTAMP,
    name                TEXT        NOT NULL,
    nickname            TEXT        NOT NULL UNIQUE,
    birth_date          DATE        NOT NULL,
    privacy_agreed_at   TIMESTAMP   NOT NULL
);

-- ============================================================
-- Table 5: user_analyses (사용자별 분석 히스토리)
-- ============================================================
CREATE TABLE IF NOT EXISTS user_analyses (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES users(id),
    analysis_id UUID        NOT NULL REFERENCES analyses(id),
    viewed_at   TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_analyses_user_id ON user_analyses (user_id);

-- ============================================================
-- Table 6: price_suggestions (가격 수정 제안)
-- ============================================================
CREATE TABLE IF NOT EXISTS price_suggestions (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID        NOT NULL REFERENCES analyses(id),
    user_id     UUID        REFERENCES users(id),
    suggestions JSONB       NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at  TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_suggestions_analysis_id ON price_suggestions (analysis_id);
CREATE INDEX IF NOT EXISTS idx_price_suggestions_status      ON price_suggestions (status);

-- ============================================================
-- Table 7: analysis_logs (분석 횟수 추적)
-- ============================================================
CREATE TABLE IF NOT EXISTS analysis_logs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ip_address  TEXT        NOT NULL,
    user_id     UUID        REFERENCES users(id),
    is_detailed BOOLEAN     NOT NULL,
    analyzed_at TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analysis_logs_ip_analyzed_at   ON analysis_logs (ip_address, analyzed_at);
CREATE INDEX IF NOT EXISTS idx_analysis_logs_user_id_analyzed_at ON analysis_logs (user_id, analyzed_at);

-- ============================================================
-- Table 8: part_price_history (부품 가격 이력)
-- ============================================================
CREATE TABLE IF NOT EXISTS part_price_history (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    part_id     UUID        NOT NULL REFERENCES parts(id),
    price_krw   INTEGER     NOT NULL,
    recorded_at TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_part_price_history_part_id_recorded_at
    ON part_price_history (part_id, recorded_at);

-- ============================================================
-- Table 9: bike_price_history (완성차 가격 이력)
-- ============================================================
CREATE TABLE IF NOT EXISTS bike_price_history (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    bike_id     UUID        NOT NULL REFERENCES bikes(id),
    price_krw   INTEGER     NOT NULL,
    recorded_at TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bike_price_history_bike_id_recorded_at
    ON bike_price_history (bike_id, recorded_at);

-- ============================================================
-- users.plan 컬럼 추가 (이미 배포된 DB에 적용 시 실행)
-- ============================================================
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'
--     CHECK (plan IN ('free', 'continental', 'pro', 'world_tour'));
