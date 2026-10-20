import logging
from datetime import timedelta
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.events import MAX_ATTEMPTS, enqueue, enqueue_payment, utcnow
from app.gateway import PaymentGateway
from app.models import Payment
from app.payments import PaymentNotFound
from app.schemas import PaymentEvent
from app.webhook import WebhookError, WebhookSender

logger = logging.getLogger(__name__)


class PaymentProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateway: PaymentGateway,
        webhook: WebhookSender,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.gateway = gateway
        self.webhook = webhook
        self.settings = settings

    async def handle(self, event: PaymentEvent) -> None:
        async with self.sessions.begin() as session:
            payment = await self._lock(session, event.payment_id)
            if not self._needs_attempt(payment, event):
                return
            if payment.status == "pending":
                try:
                    succeeded = await self.gateway.charge(payment.id)
                except Exception as exc:
                    await self._record_failure(session, payment, event, type(exc).__name__)
                    return
                payment.status = "succeeded" if succeeded else "failed"
                payment.processed_at = utcnow()
                payment.failure_reason = None if succeeded else "gateway_declined"

        async with self.sessions.begin() as session:
            payment = await self._lock(session, event.payment_id)
            if not self._needs_attempt(payment, event):
                return
            try:
                await self.webhook.send(payment)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, WebhookError) else type(exc).__name__
                await self._record_failure(session, payment, event, reason)
                return
            payment.attempts = event.attempt
            payment.webhook_delivered_at = utcnow()
            payment.last_error = None
            logger.info(
                "payment_completed",
                extra={
                    "payment_id": payment.id,
                    "event_id": event.event_id,
                    "attempt": event.attempt,
                },
            )

    async def _lock(self, session: AsyncSession, payment_id: UUID) -> Payment:
        payment = await session.scalar(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        if payment is None:
            raise PaymentNotFound
        return payment

    @staticmethod
    def _needs_attempt(payment: Payment, event: PaymentEvent) -> bool:
        return (
            payment.webhook_delivered_at is None
            and payment.exhausted_at is None
            and event.attempt == payment.attempts + 1
        )

    async def _record_failure(
        self, session: AsyncSession, payment: Payment, event: PaymentEvent, reason: str
    ) -> None:
        payment.attempts = event.attempt
        payment.last_error = reason[:128]
        now = utcnow()
        if event.attempt < MAX_ATTEMPTS:
            delay = self.settings.retry_base_delay * 2 ** (event.attempt - 1)
            await enqueue_payment(
                session, payment.id, event.attempt + 1, now + timedelta(seconds=delay)
            )
            logger.warning(
                "payment_retry_scheduled",
                extra={"payment_id": payment.id, "attempt": event.attempt, "error": reason},
            )
            return
        payment.exhausted_at = now
        if payment.status == "pending":
            payment.status = "failed"
            payment.processed_at = now
            payment.failure_reason = "processing_exhausted"
        dead_id = uuid5(payment.id, "payment.dead")
        await enqueue(
            session,
            event_id=dead_id,
            payment_id=payment.id,
            routing_key="payments.dead",
            payload={
                "event_id": str(dead_id),
                "event_type": "payment.dead",
                "payment_id": str(payment.id),
                "attempts": event.attempt,
                "status": payment.status,
                "error": payment.last_error,
                "failed_at": now.isoformat(),
                "original_event": event.model_dump(mode="json"),
            },
        )
        logger.error(
            "payment_exhausted",
            extra={"payment_id": payment.id, "attempt": event.attempt, "error": reason},
        )
