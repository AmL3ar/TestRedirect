from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.config import Settings
from app.models import Payment


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@localhost/payments_test",
        rabbitmq_url="amqp://test:test@localhost/",
        api_key="test-api-key-at-least-16",
        webhook_secret="test-webhook-secret-at-least-16",
        webhook_allowed_origins=["https://merchant.example", "http://webhook:8080"],
        gateway_min_delay=0,
        gateway_max_delay=0,
        log_level="CRITICAL",
    )


@pytest.fixture
def payment():
    return Payment(
        id=uuid4(),
        amount=Decimal("100.00"),
        currency="RUB",
        description="Заказ",
        details={"order_id": "1001"},
        status="succeeded",
        idempotency_key="test-order",
        request_hash="a" * 64,
        webhook_url="https://merchant.example/webhook",
        attempts=0,
        created_at=datetime.now(UTC),
        processed_at=datetime.now(UTC),
        webhook_delivered_at=None,
        exhausted_at=None,
        last_error=None,
        failure_reason=None,
    )


@pytest.fixture
def payload():
    return {
        "amount": "100.00",
        "currency": "RUB",
        "description": "Заказ",
        "metadata": {"order_id": "1001"},
        "webhook_url": "https://merchant.example/webhook",
    }
