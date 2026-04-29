-- ============================================================
-- 006_add_billing.sql
-- 토스페이먼츠 정기결제(빌링키) 연동을 위한 컬럼/테이블 추가
-- (idempotent — 이미 존재하면 no-op)
-- ============================================================

-- ── users: 구독/빌링 정보 ───────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at         TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_cycle      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status     TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_key             TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_customer_key    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_card_company    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_card_number     TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS next_billing_at         TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_failed_count    INTEGER NOT NULL DEFAULT 0;

ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_subscription_cycle;
ALTER TABLE users ADD  CONSTRAINT ck_users_subscription_cycle
    CHECK (subscription_cycle IS NULL OR subscription_cycle IN ('monthly', 'yearly'));

ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_subscription_status;
ALTER TABLE users ADD  CONSTRAINT ck_users_subscription_status
    CHECK (subscription_status IS NULL OR subscription_status IN ('active', 'canceled', 'past_due'));

CREATE INDEX IF NOT EXISTS idx_users_next_billing_at ON users (next_billing_at);

-- ── payments: 결제 내역 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS payments (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID        NOT NULL REFERENCES users(id),
    plan               TEXT        NOT NULL
                            CHECK (plan IN ('pro', 'world_tour')),
    cycle              TEXT        NOT NULL
                            CHECK (cycle IN ('monthly', 'yearly')),
    amount_krw         INTEGER     NOT NULL,
    status             TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'paid', 'failed', 'canceled')),
    -- 토스 응답값 (성공시 paymentKey, 우리가 발급한 orderId)
    toss_payment_key   TEXT,
    toss_order_id      TEXT        NOT NULL UNIQUE,
    failure_reason     TEXT,
    -- 자동/수동 구분: initial(첫 결제), recurring(자동결제), manual(수동 재시도)
    charge_type        TEXT        NOT NULL DEFAULT 'initial'
                            CHECK (charge_type IN ('initial', 'recurring', 'manual')),
    paid_at            TIMESTAMP,
    created_at         TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payments_user_id_created_at
    ON payments (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_payments_status
    ON payments (status);
