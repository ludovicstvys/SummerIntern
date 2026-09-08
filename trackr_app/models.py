from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="subscriber")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    preference: Mapped[Preference | None] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)
    notion: Mapped[NotionConnection | None] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    invited_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivery_status: Mapped[str] = mapped_column(String(20), default='pending', server_default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthMail(Base):
    """Durable requests, including unknown addresses; resolved only by the worker."""
    __tablename__ = 'auth_mail'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_encrypted: Mapped[str] = mapped_column(Text)
    email_hash: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    invitation_id: Mapped[int | None] = mapped_column(ForeignKey('invitations.id'), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MagicLink(Base):
    __tablename__ = "magic_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordToken(Base):
    __tablename__ = "password_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Preference(Base):
    __tablename__ = "preferences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    program_types: Mapped[str] = mapped_column(Text, default='["summer","off-cycle"]')
    regions: Mapped[str] = mapped_column(Text, default='["France","UK","Hong Kong"]')
    start_terms: Mapped[str] = mapped_column(Text, default="[]")
    delivery_mode: Mapped[str] = mapped_column(String(20), default="immediate")
    digest_time: Mapped[time] = mapped_column(Time, default=time(8, 0))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_digest_date: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    user: Mapped[User] = relationship(back_populates="preference")


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    offer_url: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text, default="")
    company_id: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(50), index=True)
    programme_type: Mapped[str] = mapped_column(String(30), index=True)
    categories: Mapped[str] = mapped_column(Text, default="[]")
    start_term: Mapped[str | None] = mapped_column(String(100), index=True)
    opening_date: Mapped[date | None] = mapped_column(Date)
    closing_date: Mapped[date | None] = mapped_column(Date)
    stage: Mapped[str] = mapped_column(String(100), default="Unknown")
    rolling: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_cv: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_cover_letter: Mapped[bool] = mapped_column(Boolean, default=False)
    company_description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    missing_collections: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sources: Mapped[list[OfferSource]] = relationship(cascade='all, delete-orphan', lazy='selectin')

    def source_label(self, attribute):
        from .config import settings
        sources = [s for s in self.sources if s.is_open and s.season == settings.season]
        return ' / '.join(sorted({getattr(s, attribute) for s in sources if getattr(s, attribute)})) or getattr(self, attribute) or ''

    @property
    def region_label(self):
        return self.source_label('region')

    @property
    def programme_label(self):
        return self.source_label('programme_type')


class OfferSource(Base):
    __tablename__ = 'offer_sources'
    __table_args__ = (UniqueConstraint('offer_id', 'region', 'programme_type', 'season'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey('offers.id', ondelete='CASCADE'), index=True)
    region: Mapped[str] = mapped_column(String(50))
    programme_type: Mapped[str] = mapped_column(String(30))
    season: Mapped[str] = mapped_column(String(20))
    start_term: Mapped[str | None] = mapped_column(String(100))
    opening_date: Mapped[date | None] = mapped_column(Date)
    closing_date: Mapped[date | None] = mapped_column(Date)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    missing_collections: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserOffer(Base):
    __tablename__ = "user_offers"
    __table_args__ = (UniqueConstraint("user_id", "offer_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), index=True)
    baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("user_id", "offer_id", "mode"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotionConnection(Base):
    __tablename__ = "notion_connections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(String(100))
    workspace_name: Mapped[str | None] = mapped_column(String(200))
    database_id: Mapped[str | None] = mapped_column(String(100))
    data_source_id: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    setup_status: Mapped[str] = mapped_column(String(20), default='idle', server_default='idle')
    parent_page_id: Mapped[str | None] = mapped_column(String(100))
    user: Mapped[User] = relationship(back_populates="notion")


class NotionSync(Base):
    __tablename__ = "notion_syncs"
    __table_args__ = (UniqueConstraint("connection_id", "offer_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("notion_connections.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerState(Base):
    __tablename__ = 'worker_states'
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class LegacyTask(Base):
    __tablename__ = 'legacy_tasks'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(160), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    recipient: Mapped[str] = mapped_column(String(320), default='')
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthLimit(Base):
    __tablename__ = "auth_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
