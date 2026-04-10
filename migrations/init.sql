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
