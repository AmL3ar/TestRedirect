from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas import PaymentCreate
from app.urls import origin_of


@pytest.mark.parametrize("amount", ["0", "-1", "1.001", "NaN", "Infinity", "10000000000000000"])
def test_invalid_amount_is_rejected(payload, amount):
    with pytest.raises(ValidationError):
        PaymentCreate(**(payload | {"amount": amount}))


def test_decimal_and_canonical_fingerprint(payload):
    first = PaymentCreate(**(payload | {"amount": "100", "metadata": {"b": 1, "a": 2}}))
    second = PaymentCreate(**(payload | {"metadata": {"a": 2, "b": 1}}))
    assert first.amount == Decimal("100.00")
    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() != PaymentCreate(**payload).fingerprint()


@pytest.mark.parametrize(
    "override",
    [
        {"currency": "BTC"},
        {"description": "x" * 1025},
        {"metadata": {"oversized": "x" * 16_385}},
        {"metadata": {"nonfinite": float("nan")}},
        {"metadata": {"nested": [{"value": "nul\x00"}]}},
        {"description": "nul\x00"},
        {"webhook_url": "file:///etc/passwd"},
        {"webhook_url": "https://user:secret@merchant.example/path"},
        {"webhook_url": "https://merchant.example/path#fragment"},
        {"unexpected": "field"},
    ],
)
def test_invalid_request_is_rejected(payload, override):
    with pytest.raises(ValidationError):
        PaymentCreate(**(payload | override))


def test_origin_comparison_is_exact():
    assert origin_of("https://MERCHANT.example:443/path?q=1") == "https://merchant.example"
    assert origin_of("https://merchant.example.evil/path") != "https://merchant.example"
    assert origin_of("http://merchant.example:8080/path") == "http://merchant.example:8080"


def test_configuration_rejects_invalid_secrets_and_origins(settings):
    values = settings.model_dump()
    for override in [
        {"api_key": "short"},
        {"webhook_allowed_origins": []},
        {"webhook_allowed_origins": ["https://merchant.example/path"]},
        {"gateway_min_delay": 5, "gateway_max_delay": 2},
    ]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **(values | override))
