from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pamqp.commands import Basic

from app.events import process_event, utcnow
from app.models import Outbox
from app.outbox import OutboxDispatcher
from app.processor import PaymentProcessor
from app.webhook import WebhookError


def sessions_returning(value):
    session = AsyncMock()
    session.scalar.return_value = value
    sessions = MagicMock()
    sessions.begin.return_value.__aenter__ = AsyncMock(return_value=session)
    sessions.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    return sessions, session


async def test_webhook_retry_does_not_repeat_charge(settings, payment, monkeypatch):
    payment.status = "pending"
    payment.processed_at = None
    sessions, session = sessions_returning(payment)
    gateway = MagicMock(charge=AsyncMock(return_value=True))
    webhook = MagicMock(send=AsyncMock(side_effect=[WebhookError("http_503"), None]))
    enqueue = AsyncMock()
    monkeypatch.setattr("app.processor.enqueue_payment", enqueue)
    processor = PaymentProcessor(sessions, gateway, webhook, settings)
    before = utcnow()
    await processor.handle(process_event(payment.id, 1))
    assert payment.status == "succeeded"
    assert payment.attempts == 1
    assert payment.webhook_delivered_at is None
    assert enqueue.await_args.args[1:3] == (payment.id, 2)
    assert before + timedelta(seconds=2) <= enqueue.await_args.args[3]
    await processor.handle(process_event(payment.id, 1))
    await processor.handle(process_event(payment.id, 2))
    gateway.charge.assert_awaited_once()
    assert webhook.send.await_count == 2
    assert payment.webhook_delivered_at is not None
    assert payment.attempts == 2
    assert payment.last_error is None


async def test_gateway_decline_is_final_and_not_retried(settings, payment):
    payment.status = "pending"
    payment.processed_at = None
    sessions, session = sessions_returning(payment)
    gateway = MagicMock(charge=AsyncMock(return_value=False))
    webhook = MagicMock(send=AsyncMock())
    await PaymentProcessor(sessions, gateway, webhook, settings).handle(
        process_event(payment.id, 1)
    )
    assert payment.status == "failed"
    assert payment.failure_reason == "gateway_declined"
    assert payment.webhook_delivered_at is not None
    session.execute.assert_not_awaited()


async def test_three_attempts_then_dlq_keep_successful_payment(settings, payment, monkeypatch):
    sessions, session = sessions_returning(payment)
    gateway = MagicMock(charge=AsyncMock())
    webhook = MagicMock(send=AsyncMock(side_effect=WebhookError("http_503")))
    enqueue_retry = AsyncMock()
    enqueue_dead = AsyncMock()
    monkeypatch.setattr("app.processor.enqueue_payment", enqueue_retry)
    monkeypatch.setattr("app.processor.enqueue", enqueue_dead)
    processor = PaymentProcessor(sessions, gateway, webhook, settings)
    for attempt in range(1, 4):
        before = utcnow()
        await processor.handle(process_event(payment.id, attempt))
        if attempt < 3:
            assert enqueue_retry.await_args.args[3] >= before + timedelta(seconds=2**attempt)
    await processor.handle(process_event(payment.id, 3))
    assert payment.status == "succeeded"
    assert payment.attempts == 3
    assert payment.exhausted_at is not None
    assert payment.webhook_delivered_at is None
    assert webhook.send.await_count == 3
    gateway.charge.assert_not_awaited()
    assert enqueue_retry.await_count == 2
    enqueue_dead.assert_awaited_once()
    assert enqueue_dead.await_args.kwargs["routing_key"] == "payments.dead"


async def test_gateway_exceptions_end_in_failed_payment(settings, payment, monkeypatch):
    payment.status = "pending"
    payment.processed_at = None
    sessions, session = sessions_returning(payment)
    gateway = MagicMock(charge=AsyncMock(side_effect=TimeoutError))
    webhook = MagicMock(send=AsyncMock())
    monkeypatch.setattr("app.processor.enqueue_payment", AsyncMock())
    monkeypatch.setattr("app.processor.enqueue", AsyncMock())
    processor = PaymentProcessor(sessions, gateway, webhook, settings)
    for attempt in range(1, 4):
        await processor.handle(process_event(payment.id, attempt))
    assert payment.status == "failed"
    assert payment.failure_reason == "processing_exhausted"
    assert payment.exhausted_at is not None
    webhook.send.assert_not_awaited()


@pytest.mark.parametrize("result", [Basic.Ack(), ConnectionError(), None])
async def test_outbox_requires_confirmation(settings, payment, result):
    event = Outbox(
        id=payment.id,
        payment_id=payment.id,
        payload={"payment_id": str(payment.id)},
        routing_key="payments.new",
        available_at=utcnow(),
        published_at=None,
        publish_attempts=0,
    )
    sessions, session = sessions_returning(event)
    publish = (
        AsyncMock(side_effect=result)
        if isinstance(result, Exception)
        else AsyncMock(return_value=result)
    )
    broker = MagicMock(publish=publish)
    assert await OutboxDispatcher(sessions, broker, settings).publish_one() is True
    assert publish.await_args.kwargs["persist"] is True
    assert publish.await_args.kwargs["mandatory"] is True
    assert publish.await_args.kwargs["message_id"] == str(event.id)
    assert event.publish_attempts == 1
    if isinstance(result, Basic.Ack):
        assert event.published_at is not None
    else:
        assert event.published_at is None
        assert event.available_at > utcnow()
