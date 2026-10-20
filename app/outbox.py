import asyncio
import logging
from datetime import timedelta

from faststream.rabbit import RabbitBroker
from pamqp.commands import Basic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker import DEAD_EXCHANGE, PAYMENTS_EXCHANGE
from app.config import Settings
from app.events import utcnow
from app.models import Outbox

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        broker: RabbitBroker,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.broker = broker
        self.settings = settings

    async def publish_one(self) -> bool:
        async with self.sessions.begin() as session:
            event = await session.scalar(
                select(Outbox)
                .where(Outbox.published_at.is_(None), Outbox.available_at <= func.now())
                .order_by(Outbox.available_at, Outbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                return False
            event.publish_attempts += 1
            exchange = DEAD_EXCHANGE if event.routing_key == "payments.dead" else PAYMENTS_EXCHANGE
            try:
                async with asyncio.timeout(self.settings.publish_timeout):
                    confirmation = await self.broker.publish(
                        event.payload,
                        exchange=exchange,
                        routing_key=event.routing_key,
                        persist=True,
                        mandatory=True,
                        timeout=self.settings.publish_timeout,
                        message_id=str(event.id),
                        correlation_id=str(event.payment_id or event.id),
                    )
                    if not isinstance(confirmation, Basic.Ack):
                        raise RuntimeError("Publisher confirmation was not received")
            except Exception as exc:
                event.last_error = type(exc).__name__
                event.available_at = utcnow() + timedelta(
                    seconds=min(2 ** min(event.publish_attempts, 6), 60)
                )
                logger.warning(
                    "outbox_publish_failed",
                    extra={"event_id": event.id, "error": event.last_error},
                )
            else:
                event.published_at = utcnow()
                event.last_error = None
                logger.info(
                    "outbox_published",
                    extra={"event_id": event.id, "routing_key": event.routing_key},
                )
            return True

    async def run(self) -> None:
        while True:
            try:
                for _ in range(self.settings.outbox_batch_size):
                    if not await self.publish_one():
                        break
            except Exception as exc:
                logger.error("outbox_iteration_failed", extra={"error": type(exc).__name__})
            await asyncio.sleep(self.settings.outbox_poll_interval)
