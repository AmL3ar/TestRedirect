import asyncio
import random
from uuid import UUID


class PaymentGateway:
    def __init__(self, min_delay: float = 2.0, max_delay: float = 5.0) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay

    async def charge(self, payment_id: UUID) -> bool:
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
        return random.Random(payment_id.int).random() < 0.9
