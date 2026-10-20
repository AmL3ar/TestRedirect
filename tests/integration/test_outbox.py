import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.broker import PAYMENTS_EXCHANGE, PAYMENTS_QUEUE
from app.models import Outbox
from app.outbox import OutboxDispatcher
from app.payments import PaymentService
from app.schemas import PaymentCreate

pytestmark = pytest.mark.integration


async def test_confirmed_message_reaches_real_rabbitmq(
    database, rabbit, integration_settings, payload
):
    payment = await PaymentService(database).create(PaymentCreate(**payload), "published")
    dispatcher = OutboxDispatcher(database, rabbit, integration_settings)
    assert await dispatcher.publish_one() is True
    assert await dispatcher.publish_one() is False
    queue = await rabbit.declare_queue(PAYMENTS_QUEUE)
    message = await queue.get(timeout=3)
    assert json.loads(message.body)["payment_id"] == str(payment.id)
    assert message.delivery_mode.value == 2
    await message.ack()
    async with database() as session:
        row = await session.scalar(select(Outbox))
        assert row.published_at is not None


async def test_unroutable_message_is_not_marked_published(
    database, rabbit, integration_settings, payload
):
    await PaymentService(database).create(PaymentCreate(**payload), "unroutable")
    queue = await rabbit.declare_queue(PAYMENTS_QUEUE)
    exchange = await rabbit.declare_exchange(PAYMENTS_EXCHANGE)
    await queue.unbind(exchange, routing_key=PAYMENTS_QUEUE.routing_key)
    try:
        await OutboxDispatcher(database, rabbit, integration_settings).publish_one()
        async with database() as session:
            row = await session.scalar(select(Outbox))
            assert row.published_at is None
            assert row.publish_attempts == 1
            assert row.last_error is not None
    finally:
        await queue.bind(exchange, routing_key=PAYMENTS_QUEUE.routing_key)


async def test_crash_after_publish_before_commit_republishes_same_event(
    database, rabbit, integration_settings, payload
):
    await PaymentService(database).create(PaymentCreate(**payload), "commit-failure")

    class FailingSession(Session):
        pass

    def fail_commit(session):
        raise RuntimeError("Commit interrupted")

    event.listen(FailingSession, "before_commit", fail_commit)
    failing_sessions = async_sessionmaker(
        database.kw["bind"],
        class_=AsyncSession,
        sync_session_class=FailingSession,
        expire_on_commit=False,
    )
    with pytest.raises(RuntimeError, match="Commit interrupted"):
        await OutboxDispatcher(failing_sessions, rabbit, integration_settings).publish_one()
    async with database() as session:
        assert (await session.scalar(select(Outbox))).published_at is None
    await OutboxDispatcher(database, rabbit, integration_settings).publish_one()
    queue = await rabbit.declare_queue(PAYMENTS_QUEUE)
    first = await queue.get(timeout=3)
    second = await queue.get(timeout=3)
    assert first.message_id == second.message_id
    await first.ack()
    await second.ack()


async def test_locked_outbox_row_does_not_block_other_events(
    database, rabbit, integration_settings, payload
):
    service = PaymentService(database)
    await service.create(PaymentCreate(**payload), "locked-first")
    second = await service.create(PaymentCreate(**payload), "unlocked-second")
    async with database.begin() as session:
        await session.scalar(select(Outbox).order_by(Outbox.created_at).with_for_update().limit(1))
        assert await OutboxDispatcher(database, rabbit, integration_settings).publish_one() is True
    queue = await rabbit.declare_queue(PAYMENTS_QUEUE)
    message = await queue.get(timeout=3)
    assert json.loads(message.body)["payment_id"] == str(second.id)
    await message.ack()
