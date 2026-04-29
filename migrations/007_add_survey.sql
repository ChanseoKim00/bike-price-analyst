-- ============================================================
-- 007_add_survey.sql
-- 결과 페이지 이탈 팝업을 설문조사 형태로 임시 전환.
-- - survey_responses: 4문항 응답(예/아니요 3 + 자유입력 1)
-- - survey_impressions: 팝업 노출 카운트 (응답률 = 응답수 / 노출수)
-- (idempotent — 이미 존재하면 no-op)
-- ============================================================

CREATE TABLE IF NOT EXISTS survey_responses (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID        REFERENCES users (id),
    q1_useful          BOOLEAN     NOT NULL,
    q2_price_diff      BOOLEAN     NOT NULL,
    q3_paid_intent     BOOLEAN     NOT NULL,
    q4_feature_request TEXT,
    created_at         TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_survey_responses_created_at ON survey_responses (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_survey_responses_user_id    ON survey_responses (user_id);

CREATE TABLE IF NOT EXISTS survey_impressions (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        REFERENCES users (id),
    created_at TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_survey_impressions_created_at ON survey_impressions (created_at DESC);
