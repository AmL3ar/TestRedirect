import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.gateway import PaymentGateway
from app.webhook import WebhookError, WebhookSender


async def test_webhook_signature_and_stable_id(settings, payment):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        sender = WebhookSender(client, settings)
        await sender.send(payment)
        payment.attempts = 1
        await sender.send(payment)
    assert len(requests) == 2
    first, second = requests
    assert first.content == second.content
    expected = hmac.new(
        settings.webhook_secret.get_secret_value().encode(), first.content, hashlib.sha256
    ).hexdigest()
    assert first.headers["X-Webhook-Signature"] == f"sha256={expected}"
    assert first.headers["X-Webhook-Id"] == second.headers["Idempotency-Key"]
    assert "X-API-Key" not in first.headers
    assert json.loads(first.content)["amount"] == "100.00"


@pytest.mark.parametrize("status", [301, 307, 400, 429, 500, 503])
async def test_non_successful_webhook_is_an_error(settings, payment, status):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(WebhookError, match=f"http_{status}"):
            await WebhookSender(client, settings).send(payment)
    assert len(requests) == 1


async def test_network_timeout_has_no_sensitive_url(settings, payment):
    def receive(request):
        raise httpx.ConnectTimeout("sensitive URL or query")

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(WebhookError, match="^ConnectTimeout$"):
            await WebhookSender(client, settings).send(payment)


async def test_total_webhook_deadline(settings, payment):
    settings.webhook_timeout = 0.01

    async def receive(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(WebhookError, match="TimeoutError"):
            await WebhookSender(client, settings).send(payment)


async def test_unapproved_url_never_reaches_transport(settings, payment):
    payment.webhook_url = "http://localhost:8000/health/ready"
    transport = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(WebhookError, match="origin_not_allowed"):
            await WebhookSender(client, settings).send(payment)
    transport.assert_not_awaited()


async def test_emulated_gateway_is_idempotent_and_sleeps_async(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.gateway.asyncio.sleep", sleep)
    monkeypatch.setattr("app.gateway.random.uniform", lambda a, b: (a + b) / 2)
    gateway = PaymentGateway()
    assert await gateway.charge(UUID(int=1)) is True
    assert await gateway.charge(UUID(int=1)) is True
    assert await gateway.charge(UUID(int=2)) is False
    assert all(call.args == (3.5,) for call in sleep.await_args_list)
