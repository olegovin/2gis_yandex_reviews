"""
database/models.py — SQLAlchemy 2.0 async models (Mapped / mapped_column syntax).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import ENUM as PgEnum


# ── Enums ─────────────────────────────────────────────────────────────────────

class ReviewSource(str, enum.Enum):
    yandex = "yandex"
    two_gis = "2gis"


class ParseStatus(str, enum.Enum):
    success = "success"
    error = "error"
    captcha_failed = "captcha_failed"


# ── Base ──────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """Shared declarative base for all models."""
    pass


# ── Models ────────────────────────────────────────────────────────────────────

class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), default=func.now()
    )

    organizations: Mapped[list[Organization]] = relationship(
        "Organization", back_populates="client", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Client id={self.id} name={self.name!r} active={self.is_active}>"


class Organization(Base):
    __tablename__ = "organizations"

    __table_args__ = (
        UniqueConstraint("client_id", "yandex_url", name="uq_org_client_yandex_url"),
        UniqueConstraint("client_id", "two_gis_url", name="uq_org_client_twogis_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)

    yandex_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    yandex_org_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    two_gis_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    two_gis_org_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), default=func.now()
    )

    client: Mapped[Client] = relationship("Client", back_populates="organizations")
    reviews: Mapped[list[Review]] = relationship(
        "Review", back_populates="organization", cascade="all, delete-orphan"
    )
    parse_logs: Mapped[list[ParseLog]] = relationship(
        "ParseLog", back_populates="organization"
    )

    def __repr__(self) -> str:
        return (
            f"<Organization id={self.id} name={self.name!r} "
            f"client_id={self.client_id} active={self.is_active}>"
        )


class Review(Base):
    __tablename__ = "reviews"

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "source", "review_hash",
            name="uq_review_org_source_hash"
        ),
        Index("ix_reviews_org_source_hash", "organization_id", "source", "review_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[ReviewSource] = mapped_column(
        PgEnum(ReviewSource, name="review_source", create_type=True),
        nullable=False,
    )
    review_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    author: Mapped[str] = mapped_column(String(512), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_date: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="Raw date string as received from the source site"
    )
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_status: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    ai_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_generated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    is_sent_to_client: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    parsed_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), default=func.now()
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="reviews")

    def __repr__(self) -> str:
        return (
            f"<Review id={self.id} source={self.source.value} "
            f"org_id={self.organization_id} rating={self.rating} "
            f"sent={self.is_sent_to_client}>"
        )


class ParseLog(Base):
    __tablename__ = "parse_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    organization_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[ReviewSource] = mapped_column(
        PgEnum(ReviewSource, name="review_source", create_type=False),
        nullable=False,
    )
    status: Mapped[ParseStatus] = mapped_column(
        PgEnum(ParseStatus, name="parse_status", create_type=True),
        nullable=False,
    )

    reviews_found: Mapped[int] = mapped_column(nullable=False, default=0)
    new_reviews_count: Mapped[int] = mapped_column(nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), default=func.now()
    )

    organization: Mapped[Optional[Organization]] = relationship(
        "Organization", back_populates="parse_logs"
    )

    def __repr__(self) -> str:
        return (
            f"<ParseLog id={self.id} org_id={self.organization_id} "
            f"source={self.source.value} status={self.status.value} "
            f"new={self.new_reviews_count} ms={self.duration_ms}>"
        )
