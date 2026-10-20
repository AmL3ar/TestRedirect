from faststream.rabbit import Channel, QueueType, RabbitBroker, RabbitExchange, RabbitQueue

from app.amqp_compat import configure_amqp_encoding
from app.config import Settings

configure_amqp_encoding()

PAYMENTS_EXCHANGE = RabbitExchange("payments", durable=True)
DEAD_EXCHANGE = RabbitExchange("payments.dlx", durable=True)
PAYMENTS_QUEUE = RabbitQueue(
    "payments.new",
    queue_type=QueueType.QUORUM,
    durable=True,
    routing_key="payments.new",
    arguments={"x-delivery-limit": -1},
)
DEAD_QUEUE = RabbitQueue(
    "payments.dlq", queue_type=QueueType.QUORUM, durable=True, routing_key="payments.dead"
)


def create_broker(settings: Settings) -> RabbitBroker:
    return RabbitBroker(
        settings.rabbitmq_url.get_secret_value(),
        default_channel=Channel(
            prefetch_count=settings.consumer_prefetch,
            publisher_confirms=True,
            on_return_raises=True,
        ),
        timeout=5,
        graceful_timeout=20,
        logger=None,
    )


async def declare_topology(broker: RabbitBroker) -> None:
    for exchange, queue in (
        (DEAD_EXCHANGE, DEAD_QUEUE),
        (PAYMENTS_EXCHANGE, PAYMENTS_QUEUE),
    ):
        declared_exchange = await broker.declare_exchange(exchange)
        declared_queue = await broker.declare_queue(queue)
        await declared_queue.bind(declared_exchange, routing_key=queue.routing_key)
