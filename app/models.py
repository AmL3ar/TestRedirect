from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_positive_amount"),
        CheckConstraint("currency IN ('RUB', 'USD', 'EUR')", name="ck_payments_currency"),
        CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="ck_payments_status"),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_payments_attempts"),
        CheckConstraint(
            "(status = 'pending' AND processed_at IS NULL) OR "
            "(status <> 'pending' AND processed_at IS NOT NULL)",
            name="ck_payments_processed_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(String(1024), default="")
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    webhook_url: Mapped[str] = mapped_column(String(2048))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(String(128))
    webhook_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exhausted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("publish_attempts >= 0", name="ck_outbox_publish_attempts"),
        CheckConstraint(
            "routing_key IN ('payments.new', 'payments.dead')", name="ck_outbox_routing_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    payment_id: Mapped[UUID | None] = mapped_column(ForeignKey("payments.id"))
    routing_key: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "ix_outbox_pending",
    Outbox.available_at,
    Outbox.created_at,
    postgresql_where=Outbox.published_at.is_(None),
)
Index("ix_outbox_payment_id", Outbox.payment_id)
