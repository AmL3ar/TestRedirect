import asyncio
import base64
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import NAMESPACE_URL, uuid5

import httpx
from faststream import AckPolicy, FastStream
from faststream.rabbit import RabbitBroker, RabbitMessage
from faststream.rabbit.message import RabbitMessage as RawRabbitMessage
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker import PAYMENTS_EXCHANGE, PAYMENTS_QUEUE, create_broker, declare_topology
from app.config import Settings, get_settings
from app.db import create_engine, create_sessions
from app.events import enqueue, process_event, utcnow
from app.gateway import PaymentGateway
from app.logging import configure_logging
from app.outbox import OutboxDispatcher
from app.payments import PaymentNotFound
from app.processor import PaymentProcessor
from app.schemas import PaymentEvent
from app.webhook import WebhookSender

logger = logging.getLogger(__name__)


def decode_raw(message: RawRabbitMessage) -> bytes:
    return bytes(message.body)


async def quarantine(sessions: async_sessionmaker[AsyncSession], body: bytes, reason: str) -> None:
    digest = hashlib.sha256(body).hexdigest()
    event_id = uuid5(NAMESPACE_URL, f"invalid-payment-event:{digest}")
    async with sessions.begin() as session:
        await enqueue(
            session,
            event_id=event_id,
            payment_id=None,
            routing_key="payments.dead",
            payload={
                "event_id": str(event_id),
                "event_type": "payment.invalid",
                "error": reason,
                "body_base64": base64.b64encode(body[:4096]).decode(),
                "body_sha256": digest,
                "truncated": len(body) > 4096,
                "failed_at": utcnow().isoformat(),
            },
        )


def register_handler(
    broker: RabbitBroker,
    processor: PaymentProcessor,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    @broker.subscriber(
        PAYMENTS_QUEUE,
        PAYMENTS_EXCHANGE,
        ack_policy=AckPolicy.MANUAL,
        decoder=decode_raw,
        no_reply=True,
    )
    async def handle_payment(body: bytes, message: RabbitMessage) -> None:
        try:
            try:
                event = PaymentEvent.model_validate_json(body)
                expected = process_event(event.payment_id, event.attempt)
                if event.event_id != expected.event_id:
                    raise ValueError("Event identifier does not match its payment and attempt")
            except (ValidationError, ValueError):
                await quarantine(sessions, body, "invalid_event")
            else:
                try:
                    await processor.handle(event)
                except PaymentNotFound:
                    await quarantine(sessions, body, "payment_not_found")
        except Exception as exc:
            logger.error("delivery_deferred", extra={"error": type(exc).__name__})
            await asyncio.sleep(settings.retry_base_delay)
            await message.nack(requeue=True)
        else:
            await message.ack()


def create_worker(settings: Settings | None = None) -> FastStream:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    sessions = create_sessions(engine)
    broker = create_broker(settings)
    client = httpx.AsyncClient(
        timeout=settings.webhook_timeout,
        limits=httpx.Limits(
            max_connections=settings.consumer_prefetch, max_keepalive_connections=10
        ),
        trust_env=False,
    )
    processor = PaymentProcessor(
        sessions,
        PaymentGateway(settings.gateway_min_delay, settings.gateway_max_delay),
        WebhookSender(client, settings),
        settings,
    )
    register_handler(broker, processor, sessions, settings)
    dispatcher = OutboxDispatcher(sessions, broker, settings)
    dispatcher_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        try:
            yield
        finally:
            if dispatcher_task is not None:
                dispatcher_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatcher_task
            await client.aclose()
            await engine.dispose()

    app = FastStream(broker, lifespan=lifespan, logger=None)

    @app.on_startup
    async def startup() -> None:
        await broker.connect()
        await declare_topology(broker)

    @app.after_startup
    async def start_dispatcher() -> None:
        nonlocal dispatcher_task
        dispatcher_task = asyncio.create_task(dispatcher.run(), name="outbox-dispatcher")

    @app.on_shutdown
    async def stop_dispatcher() -> None:
        if dispatcher_task is not None:
            dispatcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher_task

    return app


if __name__ == "__main__":
    asyncio.run(create_worker().run())
