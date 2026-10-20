import hashlib
import hmac
import json
import secrets
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings

settings = get_settings()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
events: OrderedDict[str, dict[str, Any]] = OrderedDict()
attempts: OrderedDict[str, int] = OrderedDict()


async def receive(request: Request, fail_count: int) -> JSONResponse:
    body = await request.body()
    signature = (
        "sha256="
        + hmac.new(
            settings.webhook_secret.get_secret_value().encode(), body, hashlib.sha256
        ).hexdigest()
    )
    if not secrets.compare_digest(
        request.headers.get("X-Webhook-Signature", "").encode(), signature.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    payload = json.loads(body)
    event_id = payload["event_id"]
    attempts[event_id] = attempts.get(event_id, 0) + 1
    if len(attempts) > 100:
        attempts.popitem(last=False)
    if attempts[event_id] <= fail_count:
        return JSONResponse(status_code=503, content={"detail": "Demo failure"})
    events[event_id] = payload
    if len(events) > 100:
        events.popitem(last=False)
    return JSONResponse({"received": True, "event_id": event_id})


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    return await receive(request, 0)


@app.post("/flaky")
async def flaky(request: Request) -> JSONResponse:
    return await receive(request, 2)


@app.post("/fail")
async def fail(request: Request) -> JSONResponse:
    return await receive(request, 1_000_000_000)


@app.get("/events")
async def received_events(request: Request) -> dict[str, Any]:
    if not secrets.compare_digest(
        request.headers.get("X-API-Key", "").encode(),
        settings.api_key.get_secret_value().encode(),
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return {"events": list(events.values()), "attempts": dict(attempts)}
