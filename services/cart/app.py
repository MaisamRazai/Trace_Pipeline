import asyncio
import os

import httpx
from fastapi import FastAPI

app = FastAPI(title="cart")

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")


@app.get("/cart/checkout")
async def cart_checkout():
    await asyncio.sleep(0.02)  # look up cart contents
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{INVENTORY_URL}/inventory/reserve")
        resp.raise_for_status()
    return {"service": "cart", "downstream": resp.json()}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
