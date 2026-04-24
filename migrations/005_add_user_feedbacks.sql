-- ============================================================
-- 005_add_user_feedbacks.sql
-- 유저 피드백 수집 테이블
-- (idempotent — 이미 존재하면 no-op)
-- ============================================================

CREATE TABLE IF NOT EXISTS user_feedbacks (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        REFERENCES users (id),
    rating          INTEGER     NOT NULL CHECK (rating BETWEEN 1 AND 10),
    pain_point      TEXT,
    good_point      TEXT,
    message_to_dev  TEXT,
    created_at      TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_feedbacks_created_at ON user_feedbacks (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_feedbacks_user_id    ON user_feedbacks (user_id);
