import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.api import create_app
from app.models import Outbox, Payment
from app.payments import IdempotencyConflict, PaymentService
from app.schemas import PaymentCreate

pytestmark = pytest.mark.integration


async def test_concurrent_same_key_creates_one_payment_and_event(database, payload):
    service = PaymentService(database)
    data = PaymentCreate(**payload)
    results = await asyncio.gather(*(service.create(data, "same-key") for _ in range(20)))
    assert len({result.id for result in results}) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 1
        event = await session.scalar(select(Outbox))
        assert event.payment_id == results[0].id
        assert event.routing_key == "payments.new"
        assert event.published_at is None


async def test_concurrent_conflicting_payload_returns_conflict(database, payload):
    service = PaymentService(database)
    results = await asyncio.gather(
        service.create(PaymentCreate(**payload), "conflict"),
        service.create(PaymentCreate(**(payload | {"amount": "101.00"})), "conflict"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, IdempotencyConflict) for result in results) == 1
    assert sum(isinstance(result, Payment) for result in results) == 1


async def test_outbox_failure_rolls_back_payment(database, payload, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("Outbox write failed")

    monkeypatch.setattr("app.payments.enqueue_payment", fail)
    with pytest.raises(RuntimeError, match="Outbox write failed"):
        await PaymentService(database).create(PaymentCreate(**payload), "rollback")
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 0


async def test_api_roundtrip_against_postgres(database, integration_settings, payload):
    app = create_app(integration_settings, database)
    headers = {
        "X-API-Key": integration_settings.api_key.get_secret_value(),
        "Idempotency-Key": str(uuid4()),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        first = await client.post("/api/v1/payments", json=payload, headers=headers)
        duplicate = await client.post("/api/v1/payments", json=payload, headers=headers)
        conflict = await client.post(
            "/api/v1/payments", json=payload | {"amount": "101"}, headers=headers
        )
        assert first.status_code == 202
        assert duplicate.json() == first.json()
        assert conflict.status_code == 409
        details = await client.get(first.headers["Location"], headers=headers)
        assert details.status_code == 200
        assert details.json()["status"] == "pending"
        assert details.json()["processed_at"] is None
