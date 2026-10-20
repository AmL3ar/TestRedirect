import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select

from app.events import process_event
from app.models import Outbox
from app.payments import PaymentService
from app.processor import PaymentProcessor
from app.schemas import PaymentCreate
from app.webhook import WebhookSender

pytestmark = pytest.mark.integration


async def test_concurrent_duplicate_delivery_is_serialized(database, integration_settings, payload):
    service = PaymentService(database)
    payment = await service.create(PaymentCreate(**payload), "concurrent-processing")

    async def charge(payment_id):
        await asyncio.sleep(0.02)
        return True

    gateway = MagicMock(charge=AsyncMock(side_effect=charge))
    webhook = MagicMock(send=AsyncMock())
    processor = PaymentProcessor(database, gateway, webhook, integration_settings)
    event = process_event(payment.id, 1)
    await asyncio.gather(*(processor.handle(event) for _ in range(5)))
    gateway.charge.assert_awaited_once()
    webhook.send.assert_awaited_once()
    stored = await service.get(payment.id)
    assert stored.attempts == 1
    assert stored.status == "succeeded"
    assert stored.webhook_delivered_at is not None


async def test_failure_after_status_commit_retries_only_webhook(
    database, integration_settings, payload
):
    service = PaymentService(database)
    payment = await service.create(PaymentCreate(**payload), "webhook-retry")
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200)

    gateway = MagicMock(charge=AsyncMock(return_value=True))
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        processor = PaymentProcessor(
            database, gateway, WebhookSender(client, integration_settings), integration_settings
        )
        await processor.handle(process_event(payment.id, 1))
        stored = await service.get(payment.id)
        assert stored.status == "succeeded"
        assert stored.attempts == 1
        assert stored.webhook_delivered_at is None
        async with database() as session:
            retry = await session.get(Outbox, process_event(payment.id, 2).event_id)
            assert retry is not None
            assert retry.available_at > retry.created_at
        await processor.handle(process_event(payment.id, 1))
        await processor.handle(process_event(payment.id, 2))
    gateway.charge.assert_awaited_once()
    assert len(calls) == 2
    assert calls[0].headers["X-Webhook-Id"] == calls[1].headers["X-Webhook-Id"]
    assert (await service.get(payment.id)).webhook_delivered_at is not None


async def test_exhausted_webhook_creates_one_dead_letter(database, integration_settings, payload):
    service = PaymentService(database)
    payment = await service.create(PaymentCreate(**payload), "exhausted")
    gateway = MagicMock(charge=AsyncMock(return_value=True))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        processor = PaymentProcessor(
            database, gateway, WebhookSender(client, integration_settings), integration_settings
        )
        for attempt in range(1, 4):
            await processor.handle(process_event(payment.id, attempt))
        await processor.handle(process_event(payment.id, 3))
    stored = await service.get(payment.id)
    assert stored.status == "succeeded"
    assert stored.attempts == 3
    assert stored.exhausted_at is not None
    async with database() as session:
        dead = (
            await session.scalars(select(Outbox).where(Outbox.routing_key == "payments.dead"))
        ).all()
        assert len(dead) == 1
        assert dead[0].payload["error"] == "http_503"
