from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import os
import logging
from datetime import datetime, timezone


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ags-pulse")

app = FastAPI(
    title="AGS-Pulse & GENRAI-1 Core API",
    version="1.5.1"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_KEY")
    )

    if not url or not key:
        raise RuntimeError("Supabase configuration is missing.")

    return create_client(url, key)


try:
    supabase = get_supabase_client()
except Exception:
    logger.exception("Could not initialize Supabase client.")
    raise


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def same_value(first, second):
    return clean(first).casefold() == clean(second).casefold()


def route_matches(telemetry, origin, destination, require_complete=False):
    telemetry_origin = clean(telemetry.get("origin"))
    telemetry_destination = clean(telemetry.get("destination"))

    if require_complete and (not telemetry_origin or not telemetry_destination):
        return False

    if telemetry_origin and not same_value(telemetry_origin, origin):
        return False

    if telemetry_destination and not same_value(
        telemetry_destination, destination
    ):
        return False

    return True


@app.get("/")
def root():
    return {
        "status": "online",
        "application": "AGS-Pulse",
        "engine": "GENRAI-1",
        "version": "1.5.1",
        "timestamp": now_utc()
    }


@app.get("/ping")
def ping():
    return {"ok": True}


@app.get("/health/db")
def database_health():
    try:
        supabase.table("shipment_telemetry").select("id").limit(1).execute()
        supabase.table("shipments").select("id").limit(1).execute()

        return {
            "connected": True,
            "tables": ["shipments", "shipment_telemetry"],
            "status": "healthy"
        }
    except Exception:
        logger.exception("Database health check failed.")
        raise HTTPException(
            status_code=503,
            detail="Database connection failed."
        )


@app.get("/db-test")
def db_test():
    try:
        result = (
            supabase.table("shipment_telemetry")
            .select("*")
            .limit(10)
            .execute()
        )

        return {
            "connected": True,
            "records": result.data or []
        }
    except Exception:
        logger.exception("Database test failed.")
        raise HTTPException(
            status_code=503,
            detail="Unable to retrieve shipment records."
        )


@app.get("/genrai/status")
def genrai_status():
    return {
        "engine": "GENRAI-1",
        "version": "1.5.1",
        "status": "api_online",
        "active_background_loops": False,
        "monitoring_enabled": False,
        "customs_avg_processing_minutes": None,
        "active_crossings_count": None,
        "last_sync": None
    }


def execute_genrai_telemetry_sync():
    """
    GENRAI-1 v1.5.1

    - Uses shipment_id as the primary relationship.
    - Matches older unlinked telemetry only when tracking and
      both route fields match exactly (ignoring letter case).
    - Preserves existing telemetry history.
    - Updates only the latest valid telemetry record per shipment.
    - Does not invent dwell times or delay detection.
    """

    stats = {
        "shipments_checked": 0,
        "records_created": 0,
        "records_updated": 0,
        "existing_records_linked": 0,
        "shipments_skipped": 0,
        "errors": []
    }

    current_time = now_utc()

    try:
        shipments_result = (
            supabase.table("shipments")
            .select("*")
            .execute()
        )
        shipments = shipments_result.data or []

    except Exception as exc:
        logger.exception("Unable to retrieve shipments.")
        raise RuntimeError("Could not retrieve shipments.") from exc

    for shipment in shipments:
        stats["shipments_checked"] += 1

        try:
            shipment_id = shipment.get("id")
            tracking_number = clean(shipment.get("tracking_number"))
            origin = clean(shipment.get("origin"))
            destination = clean(shipment.get("destination"))
            status = clean(shipment.get("status"))

            if (
                shipment_id is None
                or not tracking_number
                or not origin
                or not destination
                or not status
            ):
                stats["shipments_skipped"] += 1
                continue

            # First look for telemetry already linked by foreign key.
            linked_result = (
                supabase.table("shipment_telemetry")
                .select("*")
                .eq("shipment_id", shipment_id)
                .execute()
            )

            linked_rows = linked_result.data or []

            # If no linked telemetry exists, inspect older unlinked rows.
            if not linked_rows:
                legacy_result = (
                    supabase.table("shipment_telemetry")
                    .select("*")
                    .eq("rfq_id", tracking_number)
                    .is_("shipment_id", "null")
                    .execute()
                )

                legacy_rows = legacy_result.data or []

                for row in legacy_rows:
                    # Both route fields must exist and match before linking.
                    if (
                        clean(row.get("rfq_id")) == tracking_number
                        and route_matches(
                            row,
                            origin,
                            destination,
                            require_complete=True
                        )
                    ):
                        (
                            supabase.table("shipment_telemetry")
                            .update({"shipment_id": shipment_id})
                            .eq("id", row["id"])
                            .is_("shipment_id", "null")
                            .execute()
                        )

                        stats["existing_records_linked"] += 1

                # Re-read records after attempting safe legacy linking.
                linked_result = (
                    supabase.table("shipment_telemetry")
                    .select("*")
                    .eq("shipment_id", shipment_id)
                    .execute()
                )

                linked_rows = linked_result.data or []

            # Create a new record only if no safe association exists.
            if not linked_rows:
                (
                    supabase.table("shipment_telemetry")
                    .insert({
                        "shipment_id": shipment_id,
                        "rfq_id": tracking_number,
                        "origin": origin,
                        "destination": destination,
                        "status": status,
                        "delay_flag": False,
                        "last_updated": current_time,
                        "last_autonomous_check": current_time
                    })
                    .execute()
                )

                stats["records_created"] += 1
                continue

            # Keep only records whose tracking and route do not conflict.
            valid_rows = []

            for row in linked_rows:
                row_tracking = clean(row.get("rfq_id"))

                if row_tracking and not same_value(
                    row_tracking, tracking_number
                ):
                    continue

                if not route_matches(row, origin, destination):
                    continue

                valid_rows.append(row)

            if not valid_rows:
                logger.warning(
                    "Skipped shipment %s: linked telemetry has a tracking "
                    "number or route mismatch.",
                    shipment_id
                )
                stats["shipments_skipped"] += 1
                continue

            # Select the latest valid telemetry record.
            valid_rows.sort(
                key=lambda row: (
                    clean(row.get("created_at")),
                    int(row.get("id") or 0)
                ),
                reverse=True
            )

            latest = valid_rows[0]
            changes = {
                "last_autonomous_check": current_time
            }

            if not clean(latest.get("rfq_id")):
                changes["rfq_id"] = tracking_number

            if not clean(latest.get("origin")):
                changes["origin"] = origin

            if not clean(latest.get("destination")):
                changes["destination"] = destination

            status_changed = not same_value(
                latest.get("status"), status
            )

            if status_changed:
                changes["status"] = status
                changes["last_updated"] = current_time

            (
                supabase.table("shipment_telemetry")
                .update(changes)
                .eq("id", latest["id"])
                .eq("shipment_id", shipment_id)
                .execute()
            )

            stats["records_updated"] += 1

        except Exception as exc:
            logger.exception(
                "Synchronization failed for shipment ID %s.",
                shipment.get("id")
            )
            stats["errors"].append({
                "shipment_id": shipment.get("id"),
                "error": str(exc)
            })

    stats["success"] = len(stats["errors"]) == 0
    stats["completed_at"] = now_utc()

    logger.info("GENRAI-1 sync results: %s", stats)

    return stats


@app.post("/genrai/run-telemetry-sync")
def run_telemetry_sync():
    try:
        results = execute_genrai_telemetry_sync()

        return {
            "message": "GENRAI-1 v1.5.1 synchronization completed.",
            "results": results
        }

    except Exception:
        logger.exception("GENRAI-1 synchronization could not complete.")
        raise HTTPException(
            status_code=500,
            detail="GENRAI-1 synchronization failed. Check Render logs."
        )
