from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
import httpx
import os

app = FastAPI(title="AGS-Pulse GENRAI-1 Engine", version="1.0.0")

SUPABASE_URL = "https://kjqzqqhhyflrxakdqzm.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "sb_secret_CyhbmAEEzr0mXx73ST8-Qw_jJdZupNO"

headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json"
}

class TelemetryPayload(BaseModel):
    carrier: str
    status: str
    dwell_hours: float
    delay_flag: bool

@app.get("/")
def health_check():
    return {"status": "Healthy", "system": "GENRAI-1 Autonomous Engine Active"}

@app.get("/api/shipments")
async def get_shipments():
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{SUPABASE_URL}/rest/v1/shipment_telemetry?select=*", headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to fetch shipment telemetry")
        return response.json()

@app.post("/api/shipments")
async def create_shipment(payload: TelemetryPayload):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/shipment_telemetry",
            headers=headers,
            json=payload.dict()
        )
        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=500, detail="Failed to write telemetry data")
        return {"status": "success", "data": response.json()}
