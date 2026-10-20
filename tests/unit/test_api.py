from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from app.api import create_app
from app.payments import IdempotencyConflict, PaymentNotFound, PaymentService


@pytest.fixture
async def client(settings):
    app = create_app(settings, sessions=MagicMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as c:
        yield c


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/openapi.json"),
        ("POST", "/api/v1/payments"),
        ("GET", f"/api/v1/payments/{uuid4()}"),
    ],
)
async def test_all_endpoints_require_api_key(client, method, path):
    for headers in ({}, {"X-API-Key": "wrong"}):
        response = await client.request(method, path, headers=headers)
        assert response.status_code == 401


async def test_create_and_get_payment(client, settings, payload, payment, monkeypatch):
    create = AsyncMock(return_value=payment)
    monkeypatch.setattr(PaymentService, "create", create)
    monkeypatch.setattr(PaymentService, "get", AsyncMock(return_value=payment))
    headers = {"X-API-Key": settings.api_key.get_secret_value(), "Idempotency-Key": "order-1"}
    response = await client.post("/api/v1/payments", json=payload, headers=headers)
    assert response.status_code == 202
    assert response.json()["payment_id"] == str(payment.id)
    assert response.headers["Location"] == f"/api/v1/payments/{payment.id}"
    assert create.await_args.args[1] == "order-1"
    details = await client.get(response.headers["Location"], headers=headers)
    assert details.status_code == 200
    assert details.json()["amount"] == "100.00"
    assert details.json()["metadata"] == payment.details


async def test_conflicting_key_returns_409(client, settings, payload, monkeypatch):
    monkeypatch.setattr(PaymentService, "create", AsyncMock(side_effect=IdempotencyConflict))
    response = await client.post(
        "/api/v1/payments",
        json=payload,
        headers={"X-API-Key": settings.api_key.get_secret_value(), "Idempotency-Key": "order-1"},
    )
    assert response.status_code == 409


async def test_unknown_payment_returns_404(client, settings, monkeypatch):
    monkeypatch.setattr(PaymentService, "get", AsyncMock(side_effect=PaymentNotFound))
    response = await client.get(
        f"/api/v1/payments/{uuid4()}", headers={"X-API-Key": settings.api_key.get_secret_value()}
    )
    assert response.status_code == 404


@pytest.mark.parametrize("key", [None, "", "has space", "x" * 129])
async def test_invalid_idempotency_key(client, settings, payload, key):
    headers = {"X-API-Key": settings.api_key.get_secret_value()}
    if key is not None:
        headers["Idempotency-Key"] = key
    response = await client.post("/api/v1/payments", json=payload, headers=headers)
    assert response.status_code == 422


async def test_unapproved_origin_is_rejected(client, settings, payload):
    response = await client.post(
        "/api/v1/payments",
        json=payload | {"webhook_url": "http://169.254.169.254/latest/meta-data/"},
        headers={"X-API-Key": settings.api_key.get_secret_value(), "Idempotency-Key": "order-1"},
    )
    assert response.status_code == 422


async def test_openapi_declares_api_key(client, settings):
    response = await client.get(
        "/openapi.json", headers={"X-API-Key": settings.api_key.get_secret_value()}
    )
    assert response.status_code == 200
    assert response.json()["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"


async def test_unavailable_database_returns_503(client, settings, payload, monkeypatch):
    monkeypatch.setattr(
        PaymentService, "create", AsyncMock(side_effect=ConnectionRefusedError("database address"))
    )
    response = await client.post(
        "/api/v1/payments",
        json=payload,
        headers={"X-API-Key": settings.api_key.get_secret_value(), "Idempotency-Key": "order-1"},
    )
    assert response.status_code == 503
    assert "database address" not in response.text
