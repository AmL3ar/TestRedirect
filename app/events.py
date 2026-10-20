from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Outbox
from app.schemas import PaymentEvent

MAX_ATTEMPTS = 3


def utcnow() -> datetime:
    return datetime.now(UTC)


def process_event(payment_id: UUID, attempt: int) -> PaymentEvent:
    return PaymentEvent(
        event_id=uuid5(payment_id, f"process:{attempt}"),
        payment_id=payment_id,
        attempt=attempt,
    )


async def enqueue(
    session: AsyncSession,
    *,
    event_id: UUID,
    payment_id: UUID | None,
    routing_key: str,
    payload: dict[str, Any],
    available_at: datetime | None = None,
) -> None:
    await session.execute(
        insert(Outbox)
        .values(
            id=event_id,
            payment_id=payment_id,
            routing_key=routing_key,
            payload=payload,
            available_at=available_at or utcnow(),
        )
        .on_conflict_do_nothing(index_elements=[Outbox.id])
    )


async def enqueue_payment(
    session: AsyncSession, payment_id: UUID, attempt: int, available_at: datetime | None = None
) -> None:
    event = process_event(payment_id, attempt)
    await enqueue(
        session,
        event_id=event.event_id,
        payment_id=payment_id,
        routing_key="payments.new",
        payload=event.model_dump(mode="json"),
        available_at=available_at,
    )
