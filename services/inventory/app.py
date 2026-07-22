import asyncio
import os

import httpx
from fastapi import FastAPI

app = FastAPI(title="inventory")

PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8000")

# Dial this up to simulate the "checkout suddenly got slow" scenario:
# INVENTORY_EXTRA_DELAY_MS=1200 docker compose up -d --build inventory
EXTRA_DELAY_MS = int(os.getenv("INVENTORY_EXTRA_DELAY_MS", "0"))


@app.get("/inventory/reserve")
async def reserve():
    await asyncio.sleep(0.03 + EXTRA_DELAY_MS / 1000)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{PAYMENT_URL}/payment/charge")
        resp.raise_for_status()
    return {"service": "inventory", "downstream": resp.json()}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
