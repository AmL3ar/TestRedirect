import asyncio
import hashlib
import hmac
import json
from typing import Any
from uuid import uuid5

import httpx

from app.config import Settings
from app.models import Payment
from app.urls import origin_of


class WebhookError(Exception):
    pass


def webhook_payload(payment: Payment) -> dict[str, Any]:
    return {
        "event_id": str(uuid5(payment.id, "payment.completed")),
        "event_type": "payment.completed",
        "payment_id": str(payment.id),
        "amount": format(payment.amount, ".2f"),
        "currency": payment.currency,
        "description": payment.description,
        "metadata": payment.details,
        "status": payment.status,
        "failure_reason": payment.failure_reason,
        "created_at": payment.created_at.isoformat(),
        "processed_at": payment.processed_at.isoformat() if payment.processed_at else None,
    }


class WebhookSender:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def send(self, payment: Payment) -> None:
        if origin_of(payment.webhook_url) not in self.settings.webhook_allowed_origins:
            raise WebhookError("origin_not_allowed")
        payload = webhook_payload(payment)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        signature = hmac.new(
            self.settings.webhook_secret.get_secret_value().encode(), body, hashlib.sha256
        ).hexdigest()
        try:
            async with asyncio.timeout(self.settings.webhook_timeout):
                async with self.client.stream(
                    "POST",
                    payment.webhook_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Id": payload["event_id"],
                        "X-Webhook-Signature": f"sha256={signature}",
                        "Idempotency-Key": payload["event_id"],
                    },
                    follow_redirects=False,
                ) as response:
                    if not response.is_success:
                        raise WebhookError(f"http_{response.status_code}")
        except (httpx.HTTPError, TimeoutError) as exc:
            raise WebhookError(type(exc).__name__) from exc
