import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.broker import DEAD_QUEUE, PAYMENTS_EXCHANGE, PAYMENTS_QUEUE
from app.events import process_event
from app.models import Outbox
from app.outbox import OutboxDispatcher
from app.payments import PaymentService
from app.processor import PaymentProcessor
from app.schemas import PaymentCreate
from app.webhook import WebhookSender
from app.worker import register_handler

pytestmark = pytest.mark.integration


async def test_full_pipeline_three_failures_then_real_dlq(
    database, rabbit, integration_settings, payload
):
    service = PaymentService(database)
    payment = await service.create(PaymentCreate(**payload), "full-pipeline")
    gateway = MagicMock(charge=AsyncMock(return_value=True))
    received = []

    def fail(request):
        received.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        processor = PaymentProcessor(
            database, gateway, WebhookSender(client, integration_settings), integration_settings
        )
        register_handler(rabbit, processor, database, integration_settings)
        await rabbit.start()
        dispatcher = OutboxDispatcher(database, rabbit, integration_settings)
        async with asyncio.timeout(10):
            while True:
                await dispatcher.publish_one()
                async with database() as session:
                    dead = await session.scalar(
                        select(Outbox).where(Outbox.routing_key == "payments.dead")
                    )
                    if dead is not None and dead.published_at is not None:
                        break
                await asyncio.sleep(0.02)
        queue = await rabbit.declare_queue(DEAD_QUEUE)
        message = await queue.get(timeout=3)
        assert json.loads(message.body)["attempts"] == 3
        await message.ack()
    gateway.charge.assert_awaited_once()
    assert len(received) == 3
    assert len({request.headers["X-Webhook-Id"] for request in received}) == 1
    stored = await service.get(payment.id)
    assert stored.status == "succeeded"
    assert stored.exhausted_at is not None


async def test_poison_message_does_not_block_next_delivery(database, rabbit, integration_settings):
    processor = MagicMock(handle=AsyncMock())
    register_handler(rabbit, processor, database, integration_settings)
    await rabbit.start()
    await rabbit.publish(
        b"broken-json", exchange=PAYMENTS_EXCHANGE, routing_key=PAYMENTS_QUEUE.routing_key
    )
    valid_event = process_event(uuid4(), 1)
    await rabbit.publish(
        valid_event.model_dump(mode="json"),
        exchange=PAYMENTS_EXCHANGE,
        routing_key=PAYMENTS_QUEUE.routing_key,
    )
    async with asyncio.timeout(5):
        while True:
            async with database() as session:
                dead = await session.scalar(select(Outbox))
                if dead is not None and processor.handle.await_count == 1:
                    break
            await asyncio.sleep(0.02)
    assert dead.payload["error"] == "invalid_event"
    processor.handle.assert_awaited_once_with(valid_event)
