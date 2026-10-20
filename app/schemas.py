import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, field_validator

from app.urls import origin_of

Currency = Literal["RUB", "USD", "EUR"]
PaymentStatus = Literal["pending", "succeeded", "failed"]
Amount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)]


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount
    currency: Currency
    description: str = Field(default="", max_length=1024)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    webhook_url: HttpUrl = Field(max_length=2048)

    @field_validator("amount")
    @classmethod
    def normalize_amount(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        value.encode("utf-8")
        if "\x00" in value:
            raise ValueError("Null characters are not allowed")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > 16_384:
            raise ValueError("Metadata must not exceed 16 KiB")
        pending: list[JsonValue] = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("Null characters are not allowed")
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return value

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook(cls, value: HttpUrl) -> HttpUrl:
        origin_of(str(value))
        return value

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class PaymentAccepted(BaseModel):
    payment_id: UUID
    status: PaymentStatus
    created_at: datetime


class PaymentDetails(PaymentAccepted):
    amount: Decimal
    currency: Currency
    description: str
    metadata: dict[str, JsonValue]
    idempotency_key: str
    webhook_url: str
    processed_at: datetime | None
    failure_reason: str | None
    attempts: int
    webhook_delivered_at: datetime | None
    exhausted_at: datetime | None
    last_error: str | None


class PaymentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: Literal["payment.process"] = "payment.process"
    version: Literal[1] = 1
    payment_id: UUID
    attempt: int = Field(ge=1, le=3)
