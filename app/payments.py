from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events import enqueue_payment
from app.models import Payment
from app.schemas import PaymentCreate


class IdempotencyConflict(Exception):
    pass


class PaymentNotFound(Exception):
    pass


class PaymentService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create(self, data: PaymentCreate, idempotency_key: str) -> Payment:
        async with self.sessions.begin() as session:
            payment = await session.scalar(
                insert(Payment)
                .values(
                    id=uuid4(),
                    amount=data.amount,
                    currency=data.currency,
                    description=data.description,
                    details=data.metadata,
                    idempotency_key=idempotency_key,
                    request_hash=data.fingerprint(),
                    webhook_url=str(data.webhook_url),
                )
                .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
                .returning(Payment)
            )
            if payment is not None:
                await enqueue_payment(session, payment.id, 1)
                return payment
            existing = await session.scalar(
                select(Payment).where(Payment.idempotency_key == idempotency_key)
            )
            if existing is None:
                raise RuntimeError("Conflicting payment is missing")
            if existing.request_hash != data.fingerprint():
                raise IdempotencyConflict
            return existing

    async def get(self, payment_id: UUID) -> Payment:
        async with self.sessions() as session:
            payment = await session.get(Payment, payment_id)
            if payment is None:
                raise PaymentNotFound
            return payment
