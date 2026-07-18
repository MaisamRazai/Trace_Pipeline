import asyncio

from fastapi import FastAPI

app = FastAPI(title="order")


@app.get("/order/create")
async def create_order():
    await asyncio.sleep(0.02)  # persist the order
    return {"service": "order", "status": "confirmed"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
