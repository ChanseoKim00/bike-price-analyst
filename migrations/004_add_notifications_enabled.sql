-- ============================================================
-- 004_add_notifications_enabled.sql
-- users 테이블에 알림 on/off 컬럼 추가
-- (idempotent — 이미 존재하면 no-op)
-- ============================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE;
