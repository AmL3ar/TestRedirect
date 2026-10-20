from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from faststream.rabbit import TestRabbitBroker

from app.broker import PAYMENTS_EXCHANGE, PAYMENTS_QUEUE, create_broker
from app.events import process_event
from app.worker import register_handler


async def test_faststream_dispatches_valid_event(settings):
    broker = create_broker(settings)
    processor = MagicMock(handle=AsyncMock())
    register_handler(broker, processor, MagicMock(), settings)
    event = process_event(uuid4(), 1)
    async with TestRabbitBroker(broker):
        await broker.publish(
            event.model_dump(mode="json"), queue=PAYMENTS_QUEUE, exchange=PAYMENTS_EXCHANGE
        )
    processor.handle.assert_awaited_once_with(event)


@pytest.mark.parametrize("body", [b"not-json", b'{"attempt": 8}', b"[]"])
async def test_invalid_message_is_quarantined(settings, monkeypatch, body):
    broker = create_broker(settings)
    processor = MagicMock(handle=AsyncMock())
    quarantine = AsyncMock()
    monkeypatch.setattr("app.worker.quarantine", quarantine)
    register_handler(broker, processor, MagicMock(), settings)
    async with TestRabbitBroker(broker):
        await broker.publish(body, queue=PAYMENTS_QUEUE, exchange=PAYMENTS_EXCHANGE)
    processor.handle.assert_not_awaited()
    quarantine.assert_awaited_once()
    assert quarantine.await_args.args[1:] == (body, "invalid_event")
