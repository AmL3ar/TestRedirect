import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import create_engine, create_sessions
from app.logging import configure_logging
from app.payments import IdempotencyConflict, PaymentNotFound, PaymentService
from app.schemas import PaymentAccepted, PaymentCreate, PaymentDetails
from app.urls import origin_of

logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(
    settings: Settings | None = None,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings) if sessions is None else None
    if sessions is None:
        if engine is None:
            raise RuntimeError("Database engine is missing")
        sessions = create_sessions(engine)
    database_sessions = sessions
    service = PaymentService(database_sessions)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    async def authorize(key: Annotated[str | None, Security(api_key_header)]) -> None:
        if key is None or not secrets.compare_digest(
            key.encode(), settings.api_key.get_secret_value().encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    app = FastAPI(
        title="Асинхронный процессинг платежей",
        version="1.0.0",
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(OSError)
    async def database_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("database_unavailable", extra={"error": type(exc).__name__})
        return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})

    @app.post("/api/v1/payments", response_model=PaymentAccepted, status_code=202)
    async def create_payment(
        data: PaymentCreate,
        response: Response,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
        ],
    ) -> PaymentAccepted:
        if origin_of(str(data.webhook_url)) not in settings.webhook_allowed_origins:
            raise HTTPException(status_code=422, detail="Webhook origin is not allowed")
        try:
            payment = await service.create(data, idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(
                status_code=409, detail="Idempotency key is already used with another request"
            ) from exc
        response.headers["Location"] = f"/api/v1/payments/{payment.id}"
        return PaymentAccepted.model_validate(
            {
                "payment_id": payment.id,
                "status": payment.status,
                "created_at": payment.created_at,
            }
        )

    @app.get("/api/v1/payments/{payment_id}", response_model=PaymentDetails)
    async def get_payment(payment_id: UUID) -> PaymentDetails:
        try:
            payment = await service.get(payment_id)
        except PaymentNotFound as exc:
            raise HTTPException(status_code=404, detail="Payment not found") from exc
        return PaymentDetails.model_validate(
            {
                "payment_id": payment.id,
                "amount": payment.amount,
                "currency": payment.currency,
                "description": payment.description,
                "metadata": payment.details,
                "status": payment.status,
                "idempotency_key": payment.idempotency_key,
                "webhook_url": payment.webhook_url,
                "created_at": payment.created_at,
                "processed_at": payment.processed_at,
                "failure_reason": payment.failure_reason,
                "attempts": payment.attempts,
                "webhook_delivered_at": payment.webhook_delivered_at,
                "exhausted_at": payment.exhausted_at,
                "last_error": payment.last_error,
            }
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness() -> dict[str, str]:
        async with database_sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_schema() -> dict[str, Any]:
        return app.openapi()

    return app
