-- ============================================================
-- 002_add_oauth.sql
-- Google OAuth 로그인 지원 — users 테이블 확장
-- (신규 배포는 init.sql에 컬럼이 이미 포함되므로 ADD COLUMN IF NOT EXISTS로 no-op)
-- ============================================================

-- password_hash / name / birth_date: 소셜 전용 계정은 값이 없을 수 있음
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE users ALTER COLUMN name DROP NOT NULL;
ALTER TABLE users ALTER COLUMN birth_date DROP NOT NULL;

-- provider / provider_user_id 컬럼 추가
ALTER TABLE users ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_user_id TEXT;

-- provider 값 CHECK (NULL 허용)
ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_provider;
ALTER TABLE users ADD CONSTRAINT ck_users_provider
    CHECK (provider IS NULL OR provider IN ('local', 'google'));

-- (provider, provider_user_id) UNIQUE — Google 계정 중복 방지
-- Postgres는 NULL 값을 UNIQUE에서 distinct 취급하므로 기존 local 유저들은 충돌 없음
ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_provider_provider_user_id;
ALTER TABLE users ADD CONSTRAINT uq_users_provider_provider_user_id
    UNIQUE (provider, provider_user_id);
