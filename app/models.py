import uuid
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID, ARRAY, TEXT

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id                 = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email              = db.Column(db.Text, nullable=False, unique=True)
    password_hash      = db.Column(db.Text, nullable=False)
    role               = db.Column(db.Text, nullable=False, default="user")
    created_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login_at      = db.Column(db.DateTime)
    name               = db.Column(db.Text, nullable=False)
    nickname           = db.Column(db.Text, nullable=False, unique=True)
    birth_date         = db.Column(db.Date, nullable=False)
    privacy_agreed_at  = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
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
