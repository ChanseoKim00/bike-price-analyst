import uuid
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID, ARRAY, TEXT, JSONB

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id                 = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email              = db.Column(db.Text, nullable=False, unique=True)
    password_hash      = db.Column(db.Text)                      # 소셜 전용 계정은 None
    role               = db.Column(db.Text, nullable=False, default="user")
    plan               = db.Column(db.Text, nullable=False, default="continental")
    created_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login_at      = db.Column(db.DateTime)
    name               = db.Column(db.Text)                      # 소셜 계정은 None 가능
    nickname           = db.Column(db.Text, nullable=False, unique=True)
    birth_date         = db.Column(db.Date)                      # 소셜 계정은 None
    privacy_agreed_at  = db.Column(db.DateTime, nullable=False)
    provider           = db.Column(db.Text)                      # NULL | 'local' | 'google'
    provider_user_id   = db.Column(db.Text)                      # Google의 sub 값 등
    notifications_enabled = db.Column(db.Boolean, nullable=False, default=True)

    # ── 구독/빌링 (토스페이먼츠 빌링키 기반 정기결제) ──────────
    plan_expires_at      = db.Column(db.DateTime)                 # 다음 다운그레이드 예정일
    subscription_cycle   = db.Column(db.Text)                     # 'monthly' | 'yearly' | None
    subscription_status  = db.Column(db.Text)                     # 'active' | 'canceled' | 'past_due' | None
    billing_key          = db.Column(db.Text)                     # 토스 빌링키 — 자동결제 식별자
    billing_customer_key = db.Column(db.Text)                     # 토스에 보낸 customerKey (UUID 그대로)
    billing_card_company = db.Column(db.Text)                     # UI 표시용
    billing_card_number  = db.Column(db.Text)                     # 마스킹된 카드번호
    next_billing_at      = db.Column(db.DateTime)                 # 다음 자동결제 시점
    billing_failed_count = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
        db.CheckConstraint("plan IN ('continental', 'pro', 'world_tour')", name="ck_users_plan"),
        db.CheckConstraint(
            "provider IS NULL OR provider IN ('local', 'google')",
            name="ck_users_provider",
        ),
        db.CheckConstraint(
            "subscription_cycle IS NULL OR subscription_cycle IN ('monthly', 'yearly')",
            name="ck_users_subscription_cycle",
        ),
        db.CheckConstraint(
            "subscription_status IS NULL OR subscription_status IN ('active', 'canceled', 'past_due')",
            name="ck_users_subscription_status",
        ),
        db.UniqueConstraint("provider", "provider_user_id",
                            name="uq_users_provider_provider_user_id"),
        db.Index("idx_users_next_billing_at", "next_billing_at"),
    )

    def __repr__(self):
        return f"<User {self.email}>"


class Part(db.Model):
    __tablename__ = "parts"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    part_type = db.Column(
        db.String,
        nullable=False,
    )  # groupset / wheelset / frameset / saddle / handlebar
    part_name = db.Column(db.Text, nullable=False)
    part_name_normalized = db.Column(db.Text, nullable=False)
    price_krw = db.Column(db.Integer)
    official_url = db.Column(db.Text)
    last_verified_at = db.Column(db.DateTime)
    last_checked_at = db.Column(db.DateTime)
    ttl_days = db.Column(db.Integer, nullable=False, default=90)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "part_type IN ('groupset', 'wheelset', 'frameset', 'saddle', 'handlebar')",
            name="ck_parts_part_type",
        ),
        db.Index("idx_parts_last_checked_at", "last_checked_at"),
        db.Index("idx_parts_part_name_normalized", "part_name_normalized"),
    )

    def __repr__(self):
        return f"<Part {self.part_type}: {self.part_name_normalized}>"


class Bike(db.Model):
    __tablename__ = "bikes"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand = db.Column(db.Text, nullable=False)
    model_name = db.Column(db.Text, nullable=False)
    model_year = db.Column(db.Integer, nullable=False)
    price_krw = db.Column(db.Integer)
    official_url = db.Column(db.Text)
    frame_material = db.Column(db.Text, nullable=False, default="unknown")
    frame_material_confidence = db.Column(db.Float, nullable=False, default=0)
    frame_material_source = db.Column(db.Text, nullable=False, default="unknown")
    brake_type = db.Column(db.Text, nullable=False, default="unknown")
    groupset_id = db.Column(UUID(as_uuid=True), db.ForeignKey("parts.id"), nullable=False)
    wheelset_id = db.Column(UUID(as_uuid=True), db.ForeignKey("parts.id"), nullable=True)
    saddle_id = db.Column(UUID(as_uuid=True), db.ForeignKey("parts.id"), nullable=True)
    weight_kg = db.Column(db.Float)
    last_verified_at = db.Column(db.DateTime)
    stale = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    groupset = db.relationship("Part", foreign_keys=[groupset_id])
    wheelset = db.relationship("Part", foreign_keys=[wheelset_id])
    saddle = db.relationship("Part", foreign_keys=[saddle_id])

    __table_args__ = (
        db.UniqueConstraint("brand", "model_name", "model_year", name="uq_bikes_brand_model_year"),
        db.CheckConstraint(
            "frame_material IN ('carbon', 'alloy', 'steel', 'titanium', 'other', 'unknown')",
            name="ck_bikes_frame_material",
        ),
        db.CheckConstraint(
            "frame_material_source IN ('page_text', 'model_knowledge', 'unknown')",
            name="ck_bikes_frame_material_source",
        ),
        db.CheckConstraint(
            "brake_type IN ('hydraulic_disc', 'mechanical_disc', 'rim', 'unknown')",
            name="ck_bikes_brake_type",
        ),
        db.Index("idx_bikes_groupset_id", "groupset_id"),
        db.Index("idx_bikes_wheelset_id", "wheelset_id"),
        db.Index("idx_bikes_saddle_id",   "saddle_id"),
    )

    def __repr__(self):
        return f"<Bike {self.brand} {self.model_name} {self.model_year}>"


class Analysis(db.Model):
    __tablename__ = "analyses"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bike_id = db.Column(UUID(as_uuid=True), db.ForeignKey("bikes.id"), nullable=False)
    parts_sum_krw = db.Column(db.Integer, nullable=False)
    saving_krw = db.Column(db.Integer, nullable=False)
    saving_pct = db.Column(db.Float, nullable=False)
    missing_parts = db.Column(ARRAY(TEXT), nullable=False, default=list)
    # /result 렌더용 부품 스냅샷 — bikes FK에 handlebar/frameset이 없어 분석 당시 값을
    # 재구성하려면 analysis별 스냅샷이 필요. 키: groupset/wheelset/frameset/saddle/handlebar
    parts_snapshot = db.Column(JSONB)
    analyzed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    bike = db.relationship("Bike", backref="analyses")

    def __repr__(self):
        return f"<Analysis bike_id={self.bike_id} saving={self.saving_krw:,}원>"


class UserAnalysis(db.Model):
    __tablename__ = "user_analyses"

    id          = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id     = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    analysis_id = db.Column(UUID(as_uuid=True), db.ForeignKey("analyses.id"), nullable=False)
    viewed_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user     = db.relationship("User",     backref="user_analyses")
    analysis = db.relationship("Analysis", backref="user_analyses")

    __table_args__ = (
        db.Index("idx_user_analyses_user_id", "user_id"),
    )

    def __repr__(self):
        return f"<UserAnalysis user={self.user_id} analysis={self.analysis_id}>"


class PriceSuggestion(db.Model):
    __tablename__ = "price_suggestions"

    id          = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    analysis_id = db.Column(UUID(as_uuid=True), db.ForeignKey("analyses.id"), nullable=False)
    user_id     = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    suggestions = db.Column(JSONB, nullable=False)
    status      = db.Column(db.Text, nullable=False, default="pending")
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    analysis = db.relationship("Analysis", backref="price_suggestions")
    user     = db.relationship("User",     backref="price_suggestions")

    __table_args__ = (
        db.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_price_suggestions_status"),
        db.Index("idx_price_suggestions_analysis_id", "analysis_id"),
        db.Index("idx_price_suggestions_status",      "status"),
    )

    def __repr__(self):
        return f"<PriceSuggestion analysis={self.analysis_id} status={self.status}>"


class PartPriceHistory(db.Model):
    __tablename__ = "part_price_history"

    id          = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    part_id     = db.Column(UUID(as_uuid=True), db.ForeignKey("parts.id"), nullable=False)
    price_krw   = db.Column(db.Integer, nullable=False)
    recorded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    part = db.relationship("Part", backref="price_history")

    __table_args__ = (
        db.Index("idx_part_price_history_part_id_recorded_at", "part_id", "recorded_at"),
    )

    def __repr__(self):
        return f"<PartPriceHistory part={self.part_id} price={self.price_krw} at={self.recorded_at}>"


class BikePriceHistory(db.Model):
    __tablename__ = "bike_price_history"

    id          = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bike_id     = db.Column(UUID(as_uuid=True), db.ForeignKey("bikes.id"), nullable=False)
    price_krw   = db.Column(db.Integer, nullable=False)
    recorded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    bike = db.relationship("Bike", backref="price_history")

    __table_args__ = (
        db.Index("idx_bike_price_history_bike_id_recorded_at", "bike_id", "recorded_at"),
    )

    def __repr__(self):
        return f"<BikePriceHistory bike={self.bike_id} price={self.price_krw} at={self.recorded_at}>"


class AnalysisLog(db.Model):
    __tablename__ = "analysis_logs"

    id          = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ip_address  = db.Column(db.Text, nullable=False)
    user_id     = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    is_detailed = db.Column(db.Boolean, nullable=False)
    analyzed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref="analysis_logs")

    __table_args__ = (
        db.Index("idx_analysis_logs_ip_analyzed_at", "ip_address", "analyzed_at"),
        db.Index("idx_analysis_logs_user_id_analyzed_at", "user_id", "analyzed_at"),
    )

    def __repr__(self):
        return f"<AnalysisLog ip={self.ip_address} detailed={self.is_detailed} at={self.analyzed_at}>"


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id         = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id    = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    token_hash = db.Column(db.Text, nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at    = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref="password_reset_tokens")

    __table_args__ = (
        db.Index("idx_password_reset_tokens_token_hash", "token_hash"),
        db.Index("idx_password_reset_tokens_user_id_created_at", "user_id", "created_at"),
    )

    def __repr__(self):
        return f"<PasswordResetToken user={self.user_id} expires={self.expires_at}>"


class ChatbotUsageLog(db.Model):
    __tablename__ = "chatbot_usage_logs"

    id         = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    visitor_id = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index("idx_chatbot_usage_visitor_created", "visitor_id", "created_at"),
    )

    def __repr__(self):
        return f"<ChatbotUsageLog visitor={self.visitor_id} at={self.created_at}>"


class Payment(db.Model):
    __tablename__ = "payments"

    id               = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id          = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    plan             = db.Column(db.Text, nullable=False)
    cycle            = db.Column(db.Text, nullable=False)
    amount_krw       = db.Column(db.Integer, nullable=False)
    status           = db.Column(db.Text, nullable=False, default="pending")
    toss_payment_key = db.Column(db.Text)
    toss_order_id    = db.Column(db.Text, nullable=False, unique=True)
    failure_reason   = db.Column(db.Text)
    charge_type      = db.Column(db.Text, nullable=False, default="initial")
    paid_at          = db.Column(db.DateTime)
    created_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref="payments")

    __table_args__ = (
        db.CheckConstraint("plan IN ('pro', 'world_tour')",                              name="ck_payments_plan"),
        db.CheckConstraint("cycle IN ('monthly', 'yearly')",                             name="ck_payments_cycle"),
        db.CheckConstraint("status IN ('pending', 'paid', 'failed', 'canceled')",        name="ck_payments_status"),
        db.CheckConstraint("charge_type IN ('initial', 'recurring', 'manual')",          name="ck_payments_charge_type"),
        db.Index("idx_payments_user_id_created_at", "user_id", "created_at"),
        db.Index("idx_payments_status",             "status"),
    )

    def __repr__(self):
        return f"<Payment user={self.user_id} plan={self.plan} cycle={self.cycle} status={self.status}>"


class UserFeedback(db.Model):
    __tablename__ = "user_feedbacks"

    id             = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id        = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    rating         = db.Column(db.Integer, nullable=False)
    pain_point     = db.Column(db.Text)
    good_point     = db.Column(db.Text)
    message_to_dev = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref="feedbacks")

    __table_args__ = (
        db.CheckConstraint("rating BETWEEN 1 AND 10", name="ck_user_feedbacks_rating"),
        db.Index("idx_user_feedbacks_created_at", "created_at"),
        db.Index("idx_user_feedbacks_user_id",    "user_id"),
    )

    def __repr__(self):
        return f"<UserFeedback user={self.user_id} rating={self.rating}>"


class SurveyResponse(db.Model):
    """결과 페이지 이탈 설문 응답 — 4문항(예/아니요 3 + 자유입력 1)."""
    __tablename__ = "survey_responses"

    id                 = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id            = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    q1_useful          = db.Column(db.Boolean, nullable=False)  # 가격 분석 기능이 유용했나요
    q2_price_diff      = db.Column(db.Boolean, nullable=False)  # 가격이 실제와 많이 달랐나요
    q3_paid_intent     = db.Column(db.Boolean, nullable=False)  # 정확도 향상 시 유료 사용 의향
    q4_feature_request = db.Column(db.Text)                     # 추가되면 좋겠다 싶은 기능 (자유)
    created_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref="survey_responses")

    __table_args__ = (
        db.Index("idx_survey_responses_created_at", "created_at"),
        db.Index("idx_survey_responses_user_id",    "user_id"),
    )

    def __repr__(self):
        return f"<SurveyResponse user={self.user_id} at={self.created_at}>"


class SurveyImpression(db.Model):
    """설문 팝업 노출 카운트 — 응답률 계산(응답 / 노출) 분모로 사용."""
    __tablename__ = "survey_impressions"

    id         = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id    = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index("idx_survey_impressions_created_at", "created_at"),
    )

    def __repr__(self):
        return f"<SurveyImpression user={self.user_id} at={self.created_at}>"
