
import os
import logging
import httpx

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("genrai")

app = FastAPI(
    title="AGS-Pulse GENRAI-1 Engine",
    version="1.2.0"
)

SUPABASE_URL = os.getenv(
    "SUPABASE_URL", ""
).strip().rstrip("/")

SUPABASE_SECRET_KEY = (
    os.getenv("SUPABASE_SECRET_KEY") or
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
).strip()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sites.google.com"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "apikey", "Authorization"]
)

def get_supabase_headers():
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Supabase configuration missing"
        )

    if not SUPABASE_URL.startswith("https://"):
        raise HTTPException(
            status_code=500,
            detail="Invalid Supabase URL"
        )

    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json"
    }

    # Legacy JWT keys require Bearer authorization.
    # New sb_secret_ keys are not JWTs.
    if len(SUPABASE_SECRET_KEY.split(".")) == 3:
        headers["Authorization"] = (
            f"Bearer {SUPABASE_SECRET_KEY}"
        )

    return headers


class TelemetryPayload(BaseModel):
    carrier: str
    status: str
    dwell_hours: float
    delay_flag: bool

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def health_check():
    return {
        "status": "Healthy",
        "system": "GENRAI-1 Autonomous Engine Active"
    }


@app.get("/api/shipments")
async def get_shipments():
    headers = get_supabase_headers()

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/shipment_telemetry",
                params={"select": "*"},
                headers=headers
            )

            response.raise_for_status()
            return response.json()

    except httpx.HTTPStatusError as e:
        logger.error(
            "Supabase read failed: HTTP %s - %s",
            e.response.status_code,
            e.response.text
        )
        raise HTTPException(
            status_code=502,
            detail="Supabase shipment retrieval failed"
        )

    except httpx.RequestError as e:
        logger.error("Supabase connection failed: %s", str(e))
        raise HTTPException(
            status_code=502,
            detail="Unable to connect to Supabase"
        )


@app.post("/api/shipments")
async def create_shipment(payload: TelemetryPayload):
    headers = get_supabase_headers()
    headers["Prefer"] = "return=representation"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/shipment_telemetry",
                headers=headers,
                json=payload.model_dump()
            )

            response.raise_for_status()

            return {
                "status": "success",
                "data": response.json()
            }

    except httpx.HTTPStatusError as e:
        logger.error(
            "Supabase write failed: HTTP %s - %s",
            e.response.status_code,
            e.response.text
        )
        raise HTTPException(
            status_code=502,
            detail="Supabase shipment write failed"
        )

    except httpx.RequestError as e:
        logger.error("Supabase connection failed: %s", str(e))
        raise HTTPException(
            status_code=502,
            detail="Unable to connect to Supabase"
        )
