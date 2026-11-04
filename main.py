
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from supabase import create_client, Client

app = FastAPI(title="Shipment Tracking API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


@app.get("/")
def root():
    return {"status": "online"}


@app.get("/ping")
def ping():
    return {"ok": True}


@app.get("/db-test")
def db_test():
    result = (
        supabase
        .table("shipment_telemetry")
        .select("*")
        .limit(10)
        .execute()
    )

    return {
        "connected": True,
        "records": result.data
    }
