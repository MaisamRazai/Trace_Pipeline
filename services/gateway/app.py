import os

import httpx
from fastapi import FastAPI

app = FastAPI(title="gateway")

CART_URL = os.getenv("CART_URL", "http://cart:8000")


@app.get("/checkout")
async def checkout():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{CART_URL}/cart/checkout")
        resp.raise_for_status()
    return {"service": "gateway", "downstream": resp.json()}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
