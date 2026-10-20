from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.urls import origin_of


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr
    rabbitmq_url: SecretStr
    api_key: SecretStr
    webhook_secret: SecretStr
    webhook_allowed_origins: list[str] = Field(min_length=1)
    webhook_timeout: float = Field(default=5.0, gt=0, le=30)
    gateway_min_delay: float = Field(default=2.0, ge=0, le=5)
    gateway_max_delay: float = Field(default=5.0, ge=0, le=10)
    retry_base_delay: float = Field(default=2.0, gt=0, le=60)
    outbox_poll_interval: float = Field(default=0.5, gt=0, le=30)
    outbox_batch_size: int = Field(default=20, ge=1, le=100)
    publish_timeout: float = Field(default=5.0, gt=0, le=30)
    consumer_prefetch: int = Field(default=10, ge=1, le=100)
    database_pool_size: int = Field(default=15, ge=2, le=100)
    log_level: str = "INFO"

    @field_validator("api_key", "webhook_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("Secret must contain at least 16 characters")
        return value

    @field_validator("webhook_allowed_origins")
    @classmethod
    def normalize_origins(cls, values: list[str]) -> list[str]:
        return [origin_of(value, require_origin=True) for value in values]

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Unsupported log level")
        return value

    @model_validator(mode="after")
    def validate_delays(self) -> Self:
        if self.gateway_max_delay < self.gateway_min_delay:
            raise ValueError("gateway_max_delay must be >= gateway_min_delay")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
