import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.broker import DEAD_QUEUE, PAYMENTS_QUEUE, create_broker, declare_topology
from app.config import Settings


@pytest.fixture
def integration_settings(settings) -> Settings:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("Integration tests require a dedicated database ending in _test")
    values = settings.model_dump()
    values.update(
        database_url=database_url,
        rabbitmq_url=os.getenv("TEST_RABBITMQ_URL", "amqp://test:test@localhost/"),
        retry_base_delay=0.05,
        outbox_poll_interval=0.02,
    )
    return Settings(_env_file=None, **values)


@pytest.fixture
async def database(integration_settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        integration_settings.database_url.get_secret_value(), pool_size=20, max_overflow=5
    )
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE outbox, payments"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.fixture
async def rabbit(integration_settings):
    if not os.getenv("TEST_RABBITMQ_URL"):
        pytest.skip("TEST_RABBITMQ_URL is required for RabbitMQ integration tests")
    broker = create_broker(integration_settings)
    await broker.connect()
    await declare_topology(broker)
    for definition in (PAYMENTS_QUEUE, DEAD_QUEUE):
        queue = await broker.declare_queue(definition)
        await queue.purge()
    try:
        yield broker
    finally:
        await broker.stop()
