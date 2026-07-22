import asyncio
import os
import random

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

app = FastAPI(title="payment")

ORDER_URL = os.getenv("ORDER_URL", "http://order:8000")

# Dial this up to simulate a payment provider intermittently declining charges:
# PAYMENT_FAILURE_RATE=1 docker compose up -d --build payment
FAILURE_RATE = float(os.getenv("PAYMENT_FAILURE_RATE", "0"))


@app.get("/payment/charge")
async def charge():
    await asyncio.sleep(0.05)  # simulate calling out to a payment provider
    span = trace.get_current_span()

    if random.random() < FAILURE_RATE:
        # This is where auto-instrumentation runs out: FastAPI's instrumentor
        # only knows "the handler raised," not "why the provider said no." So
        # we annotate the span by hand with the business-logic reason before
        # raising, same span the HTTP framework already started for us.
        error = RuntimeError("payment provider declined the charge")
        span.record_exception(error)
        span.set_attribute("payment.outcome", "declined")
        span.set_status(Status(StatusCode.ERROR, str(error)))
        raise HTTPException(status_code=402, detail="payment declined")

    span.set_attribute("payment.outcome", "approved")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{ORDER_URL}/order/create")
        resp.raise_for_status()
    return {"service": "payment", "downstream": resp.json()}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
