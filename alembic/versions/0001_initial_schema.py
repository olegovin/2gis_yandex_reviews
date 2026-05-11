"""Initial schema: clients, organizations, reviews, parse_logs

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from __future__ import annotations
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ENUM types (must exist before tables that reference them) ─────────────
    review_source = postgresql.ENUM(
        "yandex", "2gis",
        name="review_source",
        create_type=False,  # we create explicitly below
    )
    parse_status = postgresql.ENUM(
        "success", "error", "captcha_failed",
        name="parse_status",
        create_type=False,
    )
    review_source.create(op.get_bind(), checkfirst=True)
    parse_status.create(op.get_bind(), checkfirst=True)

    # ── clients ───────────────────────────────────────────────────────────────
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_chat_id"),
    )

    # ── organizations ─────────────────────────────────────────────────────────
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("yandex_url", sa.Text(), nullable=True),
        sa.Column("yandex_org_id", sa.String(64), nullable=True),
        sa.Column("two_gis_url", sa.Text(), nullable=True),
        sa.Column("two_gis_org_id", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "yandex_url", name="uq_org_client_yandex_url"),
        sa.UniqueConstraint("client_id", "two_gis_url", name="uq_org_client_twogis_url"),
    )
    op.create_index("ix_organizations_client_id", "organizations", ["client_id"])

    # ── reviews ───────────────────────────────────────────────────────────────
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM("yandex", "2gis", name="review_source", create_type=False),
            nullable=False,
        ),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("author", sa.String(512), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("review_date", sa.String(128), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("user_status", sa.String(256), nullable=True),
        sa.Column("ai_response", sa.Text(), nullable=True),
        sa.Column("ai_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_sent_to_client", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "parsed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "source", "review_hash",
            name="uq_review_org_source_hash",
        ),
    )
    op.create_index(
        "ix_reviews_org_source_hash",
        "reviews",
        ["organization_id", "source", "review_hash"],
    )
    op.create_index("ix_reviews_organization_id", "reviews", ["organization_id"])

    # ── parse_logs ────────────────────────────────────────────────────────────
    op.create_table(
        "parse_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column(
            "source",
            postgresql.ENUM("yandex", "2gis", name="review_source", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "success", "error", "captcha_failed",
                name="parse_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("reviews_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_reviews_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_parse_logs_organization_id", "parse_logs", ["organization_id"])


def downgrade() -> None:
    op.drop_table("parse_logs")
    op.drop_index("ix_reviews_org_source_hash", table_name="reviews")
    op.drop_index("ix_reviews_organization_id", table_name="reviews")
    op.drop_table("reviews")
    op.drop_index("ix_organizations_client_id", table_name="organizations")
    op.drop_table("organizations")
    op.drop_table("clients")

    # Drop ENUMs last
    op.execute("DROP TYPE IF EXISTS parse_status")
    op.execute("DROP TYPE IF EXISTS review_source")
