import asyncio
import os

import httpx
from fastapi import FastAPI

app = FastAPI(title="payment")

ORDER_URL = os.getenv("ORDER_URL", "http://order:8000")


@app.get("/payment/charge")
async def charge():
    await asyncio.sleep(0.05)  # simulate calling out to a payment provider
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{ORDER_URL}/order/create")
    return {"service": "payment", "downstream": resp.json()}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
