from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from datetime import datetime, timezone, date
import os
import json
import logging
import secrets
import math
import hmac
import hashlib
import base64

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ags-pulse")

VERSION = "1.7.8"

DWELL_TIME_THRESHOLD_HOURS = 12
MAX_AI_SHIPMENTS = 100

TERMINAL_STATUSES = {
    "delivered",
    "completed",
    "cancelled",
    "canceled"
}

app = FastAPI(
    title="AGS-Pulse & GENRAI-1 Core API",
    version=VERSION
)

from genrai_progress import router as genrai_progress_router
app.include_router(genrai_progress_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Organization-ID", "X-GENRAI-TOKEN"]
)


# --------------------------------------------------
# SUPABASE CONNECTION
# --------------------------------------------------

def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_KEY")
    )

    if not url or not key:
        raise RuntimeError(
            "Supabase configuration is missing. "
            "Set SUPABASE_URL and SUPABASE_SECRET_KEY "
            "or SUPABASE_KEY in Render."
        )

    return create_client(url, key)


try:
    supabase = get_supabase_client()
    logger.info("Supabase client initialized successfully.")
except Exception:
    logger.exception("Could not initialize Supabase client.")
    raise


# --------------------------------------------------
# CORE UTILITIES
# --------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc).isoformat()




def record_genrai_progress(task_key, title, details, evidence=None, verification_result=None):
    """Persist the latest successful autonomous-cycle result without interrupting operations."""
    try:
        payload = {
            "task_key": task_key,
            "workstream": "genrai_autonomous_operations",
            "title": title,
            "details": details,
            "status": "completed",
            "verified_at": now_utc(),
            "verification_result": verification_result or details,
            "evidence": evidence if isinstance(evidence, dict) else {},
            "source": "GENRAI-1 autonomous runtime"
        }
        result = supabase.rpc(
            "genrai_upsert_project_progress",
            {"p_record": payload}
        ).execute()
        logger.info("GENRAI progress ledger updated: task_key=%s", task_key)
        return result.data
    except Exception:
        logger.exception("GENRAI progress ledger update failed: task_key=%s", task_key)
        return None


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def same_value(first, second):
    return clean(first).casefold() == clean(second).casefold()


def authorize_genrai(token):
    expected_token = os.getenv("GENRAI_SYNC_TOKEN", "").strip()
    supplied_token = clean(token)

    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="GENRAI authentication token is not configured."
        )

    if not supplied_token or not secrets.compare_digest(
        supplied_token,
        expected_token
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized."
        )


def resolve_dashboard_organization(authorization, requested_organization_id=""):
    """Verify the Supabase access token and require current organization membership."""
    scheme, separator, token = clean(authorization).partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="A valid Bearer access token is required.")

    try:
        auth_response = supabase.auth.get_user(token.strip())
        authenticated_user = getattr(auth_response, "user", None)
        user_id = clean(getattr(authenticated_user, "id", ""))
    except Exception:
        logger.info("Dashboard request rejected: Supabase access-token verification failed.")
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")

    try:
        memberships = (
            supabase.table("organization_memberships")
            .select("organization_id")
            .eq("user_id", user_id)
            .execute()
        ).data or []
    except Exception:
        logger.exception("Dashboard membership lookup failed.")
        raise HTTPException(status_code=503, detail="Organization access is temporarily unavailable.")

    organization_ids = list(dict.fromkeys(
        clean(row.get("organization_id")) for row in memberships
        if clean(row.get("organization_id"))
    ))

    requested = clean(requested_organization_id)
    if requested:
        if requested not in organization_ids:
            raise HTTPException(status_code=403, detail="You are not a member of this organization.")
        return requested

    if not organization_ids:
        raise HTTPException(status_code=403, detail="No organization membership is assigned to this account.")
    if len(organization_ids) > 1:
        raise HTTPException(status_code=400, detail="Select an organization to continue.")

    return organization_ids[0]


def parse_dwell_hours(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        result = float(value)

        if not math.isfinite(result) or result < 0:
            return None

        return result

    except (TypeError, ValueError, OverflowError):
        return None


def safe_row_sort_key(row):
    created_at = clean(row.get("created_at"))
    raw_id = row.get("id")

    try:
        numeric_id = int(raw_id)
    except (TypeError, ValueError, OverflowError):
        numeric_id = -1

    return (
        created_at,
        numeric_id,
        clean(raw_id)
    )


def route_matches(
    telemetry,
    origin,
    destination,
    require_complete=False
):
    telemetry_origin = clean(telemetry.get("origin"))
    telemetry_destination = clean(telemetry.get("destination"))

    if require_complete and (
        not telemetry_origin or not telemetry_destination
    ):
        return False

    if telemetry_origin and not same_value(
        telemetry_origin, origin
    ):
        return False

    if telemetry_destination and not same_value(
        telemetry_destination, destination
    ):
        return False

    return True


def is_test_telemetry_row(row):
    # Do not classify a legitimate street address such as "Test Dr" as test data.
    # Use operational status, update text, carrier, and tracking/RFQ identifiers.
    row_markers = " ".join([
        clean(row.get("status")),
        clean(row.get("status_update")),
        clean(row.get("carrier")),
        clean(row.get("rfq_id"))
    ]).casefold()
    return any(marker in row_markers for marker in ("test", "simulat", "automation", "e2e", "smoke"))


def is_production_telemetry_row(shipment, row):
    if clean(row.get("shipment_id")) != clean(shipment.get("id")):
        return False
    tracking_number = clean(shipment.get("tracking_number"))
    row_tracking = clean(row.get("rfq_id"))
    if row_tracking and not same_value(row_tracking, tracking_number):
        return False
    if not route_matches(row, shipment.get("origin"), shipment.get("destination")):
        return False
    if is_test_telemetry_row(row):
        return False
    return True


def resolve_production_telemetry_row(shipment, rows):
    production_id = shipment.get("production_telemetry_id")
    if production_id is not None:
        matches = [
            row for row in rows
            if clean(row.get("id")) == clean(production_id)
            and is_production_telemetry_row(shipment, row)
        ]
        return matches[0] if len(matches) == 1 else None

    # Assign legacy shipments only when one exact AGS tracking/RFQ row exists.
    tracking_number = clean(shipment.get("tracking_number"))
    exact_matches = [
        row for row in rows
        if same_value(row.get("rfq_id"), tracking_number)
        and is_production_telemetry_row(shipment, row)
    ]
    return exact_matches[0] if len(exact_matches) == 1 else None


def parse_estimated_delivery(value):
    if not value:
        return None

    raw = clean(value)

    try:
        parsed_date = date.fromisoformat(raw)

        return {
            "type": "date",
            "value": parsed_date
        }

    except ValueError:
        pass

    try:
        parsed_datetime = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )

        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(
                tzinfo=timezone.utc
            )

        return {
            "type": "datetime",
            "value": parsed_datetime.astimezone(timezone.utc)
        }

    except ValueError:
        return None


def delivery_is_overdue(estimated_delivery, current_time):
    parsed = parse_estimated_delivery(estimated_delivery)

    if parsed is None:
        return None

    if parsed["type"] == "date":
        return current_time.date() > parsed["value"]

    return current_time > parsed["value"]


# --------------------------------------------------
# TELEMETRY EVENT WEBHOOK
# --------------------------------------------------

@app.post("/api/v1/telemetry-event")
async def receive_telemetry_event(
    payload: dict,
    x_genrai_token: str = Header(default="")
):
    authorize_genrai(x_genrai_token)

    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="A JSON event object is required.")

    event_type = clean(payload.get("event_type") or payload.get("type"))
    if not event_type or len(event_type) > 100:
        raise HTTPException(
            status_code=400,
            detail="event_type is required and must be 100 characters or fewer."
        )

    source_system = clean(
        payload.get("source_system") or payload.get("source") or "AGS-Pulse-API"
    )[:100]
    event_id = clean(payload.get("event_id") or payload.get("id"))
    if not event_id:
        import uuid
        event_id = f"api_{uuid.uuid4().hex}"
    if len(event_id) > 200:
        raise HTTPException(status_code=400, detail="event_id is too long.")

    event_payload = payload.get("payload")
    if event_payload is None:
        event_payload = {
            key: value for key, value in payload.items()
            if key not in {"event_id", "id", "event_type", "type", "source_system", "source"}
        }
    if not isinstance(event_payload, dict) or not event_payload:
        raise HTTPException(
            status_code=400,
            detail="payload must be a non-empty JSON object containing event data."
        )

    # Idempotency: a repeated event_id returns the existing result, but a
    # reused ID with different event content is rejected.
    try:
        existing_result = (
            supabase.table("genrai_telemetry_events")
            .select("event_id,source_system,event_type,payload,status,analyzed_at,engine_verdict,last_processing_error")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        existing_rows = existing_result.data or []
        if existing_rows:
            existing = existing_rows[0]
            if (
                existing.get("source_system") != source_system
                or existing.get("event_type") != event_type
                or existing.get("payload") != event_payload
            ):
                raise HTTPException(
                    status_code=409,
                    detail="event_id already exists with different event content."
                )
            return {
                "success": True,
                "duplicate": True,
                "event_id": existing.get("event_id"),
                "status": existing.get("status"),
                "engine_verdict": existing.get("engine_verdict"),
                "last_processing_error": existing.get("last_processing_error"),
                "persisted": True
            }

        supabase.table("genrai_telemetry_events").insert({
            "event_id": event_id,
            "source_system": source_system,
            "event_type": event_type,
            "payload": event_payload,
            "status": "PENDING_ANALYSIS"
        }).execute()

        # The AFTER INSERT trigger runs the GENRAI processor and updates the
        # same row. Re-read to report the database's actual persisted result.
        processed_result = (
            supabase.table("genrai_telemetry_events")
            .select("event_id,status,received_at,analyzed_at,engine_verdict,last_processing_error")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        processed_rows = processed_result.data or []
        if not processed_rows:
            raise RuntimeError("Inserted event could not be read back.")
        event = processed_rows[0]
        if event.get("status") in {"ANALYZED", "REJECTED"}:
            record_genrai_progress(
                "genrai-telemetry-event-processing",
                "GENRAI telemetry event processing",
                "Latest telemetry event processed by the GENRAI event-ingestion path.",
                evidence={
                    "event_id": event.get("event_id"),
                    "event_status": event.get("status"),
                    "analyzed_at": event.get("analyzed_at")
                },
                verification_result="Event verdict/status was read back from Supabase after processing."
            )

        return {
            "success": event.get("status") in {"ANALYZED", "REJECTED", "PENDING_ANALYSIS"},
            "duplicate": False,
            "event_id": event.get("event_id"),
            "status": event.get("status"),
            "received_at": event.get("received_at"),
            "analyzed_at": event.get("analyzed_at"),
            "engine_verdict": event.get("engine_verdict"),
            "last_processing_error": event.get("last_processing_error"),
            "persisted": True
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("GENRAI telemetry event persistence failed: event_id=%s", event_id)
        raise HTTPException(
            status_code=503,
            detail="Unable to persist or retrieve the telemetry event."
        )



# --------------------------------------------------
# AFTERSHIP TRACKING WEBHOOK
# --------------------------------------------------

@app.post("/integrations/aftership/webhook")
async def receive_aftership_webhook(request: Request):
    # Verify AfterShip's native HMAC-SHA256 signature against the exact raw body.
    secret = os.getenv("AFTERSHIP_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="AfterShip webhook secret is not configured.")

    body = await request.body()
    received_signature = request.headers.get("aftership-hmac-sha256", "").strip()
    expected_signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    if not received_signature or not hmac.compare_digest(expected_signature, received_signature):
        raise HTTPException(status_code=401, detail="Invalid AfterShip signature.")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON webhook payload.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="A JSON webhook object is required.")

    event_name = clean(payload.get("event"))
    if event_name not in {"tracking_update", "edd_revise", "tracking_pending_time"}:
        return {"success": True, "ignored": True, "reason": "Unsupported AfterShip event."}

    msg = payload.get("msg")
    if not isinstance(msg, dict):
        msg = {}

    # Accept the currently observed flat msg shape and nested tracking variants.
    tracking_obj = msg.get("tracking")
    if not isinstance(tracking_obj, dict):
        tracking_obj = payload.get("tracking")
    if not isinstance(tracking_obj, dict):
        tracking_obj = msg if msg else payload

    carrier_tracking_number = clean(
        tracking_obj.get("tracking_number") or msg.get("tracking_number")
    )
    carrier_slug = clean(
        tracking_obj.get("slug") or msg.get("slug")
    ).casefold()[:100]
    internal_reference = clean(
        tracking_obj.get("title") or msg.get("title")
    )

    if not carrier_tracking_number and not internal_reference:
        raise HTTPException(
            status_code=400,
            detail="AfterShip payload is missing a carrier tracking number and internal reference."
        )

    provider_event_id = clean(
        payload.get("event_id")
        or tracking_obj.get("event_id")
        or msg.get("event_id")
        or msg.get("id")
    )
    event_fingerprint = hashlib.sha256(body).hexdigest()
    genrai_event_id = (
        "aftership_" + provider_event_id
        if provider_event_id else "aftership_" + event_fingerprint
    )[:200]

    try:
        # Atomically claim this provider event. Only the request that inserts
        # a new receipt or takes over an expired/failed lease may process it.
        claim_rows = supabase.rpc(
            "claim_aftership_webhook_receipt",
            {
                "p_provider_event_key": genrai_event_id,
                "p_event_fingerprint": event_fingerprint
            }
        ).execute().data or []
        claim = claim_rows[0] if claim_rows else {}
        claim_status = claim.get("claim_status")
        linked_id = claim.get("linked_genrai_event_id") or genrai_event_id

        if claim_status == "COMPLETE":
            linked = (
                supabase.table("genrai_telemetry_events")
                .select("event_id,status,analyzed_at,engine_verdict,last_processing_error")
                .eq("event_id", linked_id)
                .limit(1)
                .execute()
            ).data or []
            prior = linked[0] if linked else {}
            return {
                "success": True,
                "duplicate": True,
                "event_id": linked_id,
                "status": prior.get("status", "COMPLETE"),
                "analyzed_at": prior.get("analyzed_at"),
                "engine_verdict": prior.get("engine_verdict"),
                "last_processing_error": prior.get("last_processing_error"),
                "processing_pending": prior.get("status") in {"PENDING_ANALYSIS", "PROCESSING"}
            }

        if claim_status == "PROCESSING":
            return {
                "success": True,
                "duplicate": True,
                "processing_pending": True,
                "event_id": linked_id,
                "status": "PROCESSING"
            }

        if claim_status != "CLAIMED":
            raise RuntimeError(
                "Atomic AfterShip receipt claim returned an unexpected state."
            )

        existing = (
            supabase.table("genrai_telemetry_events")
            .select("event_id,status,event_type,analyzed_at,engine_verdict,last_processing_error")
            .eq("event_id", genrai_event_id)
            .limit(1)
            .execute()
        ).data or []
        unmatched_existing = bool(
            existing and existing[0].get("status") == "UNMATCHED"
            and existing[0].get("event_type") == "AFTERSHIP_UNMATCHED"
        )
        if unmatched_existing:
            logger.info("Retrying retained unmatched AfterShip event: event_id=%s", genrai_event_id)

        # Internal title is accepted only when it exactly identifies an AGS
        # shipments.tracking_number. Otherwise resolve by the saved carrier map.
        internal_matches = []
        if internal_reference:
            internal_matches = (
                supabase.table("shipments")
                .select("id,tracking_number,status,origin,destination,carrier,carrier_slug,carrier_tracking_number,production_telemetry_id,organization_id")
                .eq("tracking_number", internal_reference)
                .execute()
            ).data or []

        carrier_matches = []
        if carrier_tracking_number:
            carrier_query = (
                supabase.table("shipments")
                .select("id,tracking_number,status,origin,destination,carrier,carrier_slug,carrier_tracking_number,production_telemetry_id,organization_id")
                .eq("carrier_tracking_number", carrier_tracking_number)
            )
            if carrier_slug:
                carrier_query = carrier_query.eq("carrier_slug", carrier_slug)
            carrier_matches = carrier_query.execute().data or []

        if len(internal_matches) > 1 or len(carrier_matches) > 1:
            logger.error(
                "AfterShip mapping conflict: internal_reference_present=%s carrier_number_present=%s",
                bool(internal_reference), bool(carrier_tracking_number)
            )
            raise HTTPException(status_code=409, detail="Multiple shipments match the AfterShip identifiers.")

        if internal_matches and carrier_matches:
            if internal_matches[0]["id"] != carrier_matches[0]["id"]:
                raise HTTPException(status_code=409, detail="Internal reference and carrier mapping identify different shipments.")

        matches = internal_matches or carrier_matches
        if not matches:
            unmatched_reason = "No shipment matched the supplied internal reference or saved carrier mapping."
            unmatched_payload = {
                "provider": "AfterShip",
                "provider_event_id": provider_event_id or None,
                "event": event_name,
                "carrier_slug": carrier_slug or None,
                "carrier_tracking_number": carrier_tracking_number or None,
                "internal_reference": internal_reference or None,
                "provider_payload": payload
            }
            if unmatched_existing:
                supabase.table("genrai_telemetry_events").update({
                    "payload": unmatched_payload,
                    "last_processing_error": unmatched_reason
                }).eq("event_id", genrai_event_id).eq("status", "UNMATCHED").execute()
            else:
                supabase.table("genrai_telemetry_events").insert({
                    "event_id": genrai_event_id,
                    "source_system": "AfterShip",
                    "event_type": "AFTERSHIP_UNMATCHED",
                    "payload": unmatched_payload,
                    "status": "UNMATCHED",
                    "last_processing_error": unmatched_reason
                }).execute()
            supabase.table("aftership_webhook_receipts").update({
                "status": "COMPLETE",
                "genrai_event_id": genrai_event_id,
                "completed_at": now_utc(),
                "last_error": unmatched_reason
            }).eq("provider_event_key", genrai_event_id).execute()
            logger.warning(
                "AfterShip event retained unmatched: event_id=%s carrier_slug=%s carrier_tracking_number_present=%s internal_reference_present=%s",
                genrai_event_id, carrier_slug or None, bool(carrier_tracking_number), bool(internal_reference)
            )
            return {
                "success": True,
                "unmatched": True,
                "persisted": True,
                "status": "UNMATCHED",
                "event_id": genrai_event_id,
                "reason": unmatched_reason
            }

        shipment = matches[0]
        shipment_id = shipment["id"]

        # Before saving a new carrier mapping, make sure it is not already
        # assigned to a different shipment (including slug-less payloads).
        if carrier_tracking_number:
            conflict_query = (
                supabase.table("shipments")
                .select("id")
                .eq("carrier_tracking_number", carrier_tracking_number)
            )
            if carrier_slug:
                conflict_query = conflict_query.eq("carrier_slug", carrier_slug)
            conflicts = conflict_query.execute().data or []
            if any(row.get("id") != shipment_id for row in conflicts):
                raise HTTPException(status_code=409, detail="Carrier tracking number is already mapped to another shipment.")

        production_telemetry_id = shipment.get("production_telemetry_id")
        if production_telemetry_id is None:
            raise HTTPException(
                status_code=409,
                detail="Shipment has no explicit production telemetry reference."
            )

        telemetry_rows = (
            supabase.table("shipment_telemetry")
            .select(
                "id,shipment_id,dwell_hours,delay_flag,rfq_id,origin,destination,"
                "status,hub_location,status_update,created_at,last_updated"
            )
            .eq("id", production_telemetry_id)
            .eq("shipment_id", shipment_id)
            .limit(2)
            .execute()
        ).data or []
        telemetry = resolve_production_telemetry_row(shipment, telemetry_rows)
        if telemetry is None:
            raise HTTPException(
                status_code=409,
                detail="Explicit production telemetry reference is missing or invalid; refusing an unsafe write."
            )
        telemetry_id = telemetry["id"]
        supabase.table("aftership_webhook_receipts").update({
            "shipment_id": shipment_id,
            "telemetry_id": telemetry_id
        }).eq("provider_event_key", genrai_event_id).execute()

        checkpoints = tracking_obj.get("checkpoints") or msg.get("checkpoints") or []
        checkpoints = [item for item in checkpoints if isinstance(item, dict)] if isinstance(checkpoints, list) else []

        # AfterShip exposes carrier event time as checkpoint_time and ingestion
        # time as created_at. Sort explicitly; do not assume provider array order.
        def checkpoint_sort_key(item):
            for field in ("checkpoint_time", "created_at"):
                raw_time = clean(item.get(field))
                if not raw_time:
                    continue
                try:
                    parsed_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    if parsed_time.tzinfo is None:
                        parsed_time = parsed_time.replace(tzinfo=timezone.utc)
                    return (0, parsed_time.timestamp())
                except (TypeError, ValueError, OverflowError):
                    continue
            return (1, 0)

        checkpoint_keys = [checkpoint_sort_key(item) for item in checkpoints]
        # If any checkpoint lacks a usable event/ingestion timestamp, retain
        # the provider-supplied order rather than inventing chronology.
        if checkpoints and all(key[0] == 0 for key in checkpoint_keys):
            checkpoints = [
                item for _, item in sorted(
                    zip(checkpoint_keys, checkpoints),
                    key=lambda pair: pair[0][1]
                )
            ]
        latest_checkpoint = checkpoints[-1] if checkpoints else {}

        raw_tag = clean(
            tracking_obj.get("tag") or tracking_obj.get("status")
            or msg.get("tag") or msg.get("status")
            or latest_checkpoint.get("tag")
        )
        operational_status = clean(
            latest_checkpoint.get("subtag_message")
            or tracking_obj.get("subtag_message")
            or msg.get("subtag_message")
            or raw_tag
            or event_name
        )[:200]
        location = clean(
            latest_checkpoint.get("location")
            or latest_checkpoint.get("city")
            or tracking_obj.get("destination_city")
            or msg.get("destination_city")
        )[:250]
        carrier = carrier_slug or clean(shipment.get("carrier"))[:100]
        delay_flag = raw_tag.casefold().replace("_", "") in {
            "exception", "failedattempt", "expired", "deliveryexception"
        }

        estimated_data = (
            tracking_obj.get("latest_estimated_delivery")
            or msg.get("latest_estimated_delivery")
            or tracking_obj.get("aftership_estimated_delivery_date")
            or msg.get("aftership_estimated_delivery_date")
            or tracking_obj.get("courier_estimated_delivery_date")
            or msg.get("courier_estimated_delivery_date")
            or {}
        )
        estimated = (
            estimated_data.get("datetime")
            or estimated_data.get("estimated_delivery_date")
            if isinstance(estimated_data, dict) else None
        )
        estimated = clean(estimated)
        if estimated and len(estimated) == 10:
            estimated += "T00:00:00+00:00"

        checkpoint_message = clean(
            latest_checkpoint.get("message")
            or latest_checkpoint.get("subtag_message")
            or operational_status
        )[:1000]
        timestamp = now_utc()

        # A multi-leg shipment has multiple active mapping rows by design.
        # They are legs of one route, not competing shipment mappings.
        route_mappings = (
            supabase.rpc(
                "get_active_aftership_route_mappings",
                {"p_shipment_id": shipment_id}
            ).execute()
        ).data or []
        route_ids = {clean(row.get("route_id")) for row in route_mappings if clean(row.get("route_id"))}
        if len(route_ids) > 1:
            raise HTTPException(
                status_code=409,
                detail="Shipment has multiple active AfterShip route IDs; operator route selection is required."
            )
        leg_sequences = [row.get("leg_sequence") for row in route_mappings]
        if len(leg_sequences) != len(set(leg_sequences)):
            raise HTTPException(
                status_code=409,
                detail="Active AfterShip route contains duplicate leg_sequence values."
            )

        # AfterShip's current checkpoint country field is country_region
        # (ISO alpha-3); retain legacy aliases for older webhook versions.
        iso2_to_iso3 = {
            "US": "USA", "MX": "MEX", "CA": "CAN", "GB": "GBR",
            "DE": "DEU", "FR": "FRA", "ES": "ESP", "IT": "ITA",
            "NL": "NLD", "BE": "BEL", "CN": "CHN", "JP": "JPN",
            "KR": "KOR", "AU": "AUS", "BR": "BRA", "IN": "IND"
        }

        def normalize_country(value):
            code = clean(value).upper()
            return iso2_to_iso3.get(code, code)

        international_context = None
        if route_mappings and len(checkpoints) >= 2:
            previous_checkpoint = checkpoints[-2]
            previous_country = normalize_country(
                previous_checkpoint.get("country_region")
                or previous_checkpoint.get("country_iso3")
                or previous_checkpoint.get("country_code")
            )
            current_country = normalize_country(
                latest_checkpoint.get("country_region")
                or latest_checkpoint.get("country_iso3")
                or latest_checkpoint.get("country_code")
            )

            # Match the newest chronological checkpoint transition to its
            # specific route leg. Multiple rows for one route are expected.
            matching_legs = [
                row for row in route_mappings
                if normalize_country(row.get("origin_country")) == previous_country
                and normalize_country(row.get("destination_country")) == current_country
            ]
            if len(matching_legs) > 1:
                raise HTTPException(
                    status_code=409,
                    detail="Latest AfterShip country transition matches multiple route legs."
                )
            if matching_legs:
                route_mapping = matching_legs[0]
                international_context = {
                    "route_id": route_mapping["route_id"],
                    "shipment_reference": shipment.get("tracking_number"),
                    "origin_country": route_mapping["origin_country"],
                    "destination_country": route_mapping["destination_country"],
                    "transport_mode": route_mapping["transport_mode"],
                    "leg_sequence": route_mapping["leg_sequence"],
                    "leg_status": operational_status[:100],
                    "handoff_status": "UNKNOWN"
                }

        event_payload = {
            "id": telemetry_id,
            "shipment_id": shipment_id,
            "internal_reference": shipment.get("tracking_number"),
            "carrier_tracking_number": carrier_tracking_number or None,
            "carrier_slug": carrier_slug or None,
            "event": event_name,
            "event_status": operational_status,
            "raw_tag": raw_tag,
            "location": location,
            "delay_flag": delay_flag,
            "dwell_hours": telemetry.get("dwell_hours"),
            "provider": "AfterShip",
            "provider_event_id": provider_event_id or None
        }
        if international_context:
            event_payload["international"] = international_context

        telemetry_update = {
            "status": operational_status,
            "hub_location": location or None,
            "status_update": checkpoint_message,
            "carrier": carrier or None,
            "delay_flag": delay_flag,
            "last_updated": timestamp
        }
        if estimated:
            telemetry_update["estimated_delivery"] = estimated
        supabase.table("shipment_telemetry").update(
            telemetry_update
        ).eq("id", telemetry_id).eq("shipment_id", shipment_id).execute()

        shipment_update = {
            "status": operational_status,
            "carrier": carrier or shipment.get("carrier"),
            "updated_at": timestamp
        }
        if carrier_slug:
            shipment_update["carrier_slug"] = carrier_slug
        if carrier_tracking_number:
            shipment_update["carrier_tracking_number"] = carrier_tracking_number
        if location:
            shipment_update["hub_location"] = location
        if checkpoint_message:
            shipment_update["status_update"] = checkpoint_message
        if estimated:
            shipment_update["estimated_delivery"] = estimated
        supabase.table("shipments").update(
            shipment_update
        ).eq("id", shipment_id).execute()

        # Persist or promote the provider event into the GENRAI queue.
        # The database trigger owns analysis and shipment-verdict write-back.
        genrai_event_record = {
            "organization_id": shipment.get("organization_id"),
            "source_system": "AfterShip",
            "event_type": (
                "CROSS_BORDER_TRANSIT"
                if international_context else "SHIPMENT_TELEMETRY_UPDATE"
            ),
            "payload": event_payload,
            "status": "PENDING_ANALYSIS",
            "analyzed_at": None,
            "engine_verdict": None,
            "last_processing_error": None,
            "retry_attempts": 0,
            "next_retry_at": timestamp
        }
        if unmatched_existing:
            supabase.table("genrai_telemetry_events").update(
                genrai_event_record
            ).eq("event_id", genrai_event_id).eq("status", "UNMATCHED").execute()
        else:
            genrai_event_record["event_id"] = genrai_event_id
            supabase.table("genrai_telemetry_events").insert(
                genrai_event_record
            ).execute()

        processed = (
            supabase.table("genrai_telemetry_events")
            .select("event_id,status,analyzed_at,engine_verdict,last_processing_error")
            .eq("event_id", genrai_event_id)
            .limit(1)
            .execute()
        ).data or []
        if not processed:
            raise RuntimeError("GENRAI event could not be read back after insertion.")
        result = processed[0]
        result_status = result.get("status")
        supabase.table("aftership_webhook_receipts").update({
            "status": "COMPLETE",
            "genrai_event_id": genrai_event_id,
            "completed_at": now_utc(),
            "last_error": None
        }).eq("provider_event_key", genrai_event_id).execute()
        return {
            "success": result_status in {"ANALYZED", "REJECTED", "PENDING_ANALYSIS", "PROCESSING"},
            "accepted": True,
            "duplicate": False,
            "processing_pending": result_status in {"PENDING_ANALYSIS", "PROCESSING"},
            "event_id": genrai_event_id,
            "status": result_status,
            "analyzed_at": result.get("analyzed_at"),
            "engine_verdict": result.get("engine_verdict"),
            "last_processing_error": result.get("last_processing_error"),
            "shipment_id": shipment_id,
            "telemetry_id": telemetry_id,
            "shipment_updated": True,
            "carrier_tracking_number_saved": bool(carrier_tracking_number),
            "carrier_slug_saved": bool(carrier_slug)
        }

    except HTTPException:
        try:
            supabase.table("aftership_webhook_receipts").update({
                "status": "FAILED",
                "last_error": "Webhook processing failed; retry is permitted."
            }).eq("provider_event_key", genrai_event_id).eq("status", "PROCESSING").execute()
        except Exception:
            logger.exception("Failed to release AfterShip receipt after HTTP error.")
        raise
    except Exception:
        try:
            supabase.table("aftership_webhook_receipts").update({
                "status": "FAILED",
                "last_error": "Webhook processing failed; retry is permitted."
            }).eq("provider_event_key", genrai_event_id).eq("status", "PROCESSING").execute()
        except Exception:
            logger.exception("Failed to release AfterShip receipt after processing error.")
        logger.exception("AfterShip webhook processing failed.")
        raise HTTPException(status_code=503, detail="AfterShip event processing failed.")


# --------------------------------------------------

# GENRAI-1 LOCAL RULES ENGINE
# --------------------------------------------------

def execute_local_rules_engine(report):

    shipments = report.get("shipments", [])
    findings = []

    for index, shipment in enumerate(shipments, start=1):

        reference = f"S{index:03d}"

        state = shipment.get("monitoring_state")
        overdue = shipment.get("overdue_estimated_delivery")
        missing = shipment.get("missing_information", [])

        if state == "ALERT":

            reasons = []

            if shipment.get("dwell_threshold_exceeded"):
                reasons.append(
                    "Recorded dwell time exceeds the configured threshold."
                )

            if shipment.get("existing_delay_flag"):
                reasons.append(
                    "An existing telemetry delay flag is present."
                )

            if overdue is True:
                reasons.append(
                    "Estimated delivery is overdue."
                )

            findings.append({
                "reference": reference,
                "finding": "Shipment requires operational attention.",
                "recommended_action": (
                    "Verify the latest shipment status and contact "
                    "the responsible hub or carrier."
                ),
                "reasons": reasons
            })

        elif state == "UNKNOWN":

            findings.append({
                "reference": reference,
                "finding": "Insufficient monitoring information.",
                "recommended_action": (
                    "Obtain or verify the missing telemetry information."
                ),
                "reasons": missing
            })

    if not findings:
        findings.append({
            "reference": "SYSTEM",
            "finding": "No active shipment alerts were identified.",
            "recommended_action": "Continue routine monitoring.",
            "reasons": []
        })

    return {
        "engine": "GENRAI-1",
        "version": VERSION,
        "source": "LOCAL_RULES_ENGINE",
        "executive_summary": (
            f"Checked {report.get('shipments_checked', 0)} shipments. "
            f"Found {report.get('alerts_recommended', 0)} alerts "
            f"and {report.get('unknown_shipments', 0)} "
            "shipments requiring additional information."
        ),
        "priority_findings": findings,
        "database_modified": False,
        "generated_at": now_utc()
    }


# --------------------------------------------------
# OPENAI REASONING ENGINE
# --------------------------------------------------

def build_ai_input(report):

    shipment_inputs = []
    reference_map = {}

    shipments = report.get("shipments", [])

    for index, shipment in enumerate(
        shipments[:MAX_AI_SHIPMENTS], start=1
    ):

        reference = f"S{index:03d}"

        reference_map[reference] = {
            "shipment_id": shipment.get("shipment_id"),
            "tracking_number": shipment.get("tracking_number")
        }

        shipment_inputs.append({
            "reference": reference,
            "master_status": shipment.get("master_status"),
            "monitoring_state": shipment.get("monitoring_state"),
            "recorded_dwell_hours": shipment.get(
                "recorded_dwell_hours"
            ),
            "dwell_threshold_hours": shipment.get(
                "dwell_threshold_hours"
            ),
            "dwell_threshold_exceeded": shipment.get(
                "dwell_threshold_exceeded"
            ),
            "existing_delay_flag": shipment.get(
                "existing_delay_flag"
            ),
            "overdue_estimated_delivery": shipment.get(
                "overdue_estimated_delivery"
            ),
            "missing_information": shipment.get(
                "missing_information", []
            )
        })

    payload = {
        "engine": "GENRAI-1",
        "mode": "READ_ONLY_ANALYSIS",
        "monitoring_totals": {
            "shipments_checked": report.get("shipments_checked", 0),
            "clear_shipments": report.get("clear_shipments", 0),
            "unknown_shipments": report.get("unknown_shipments", 0),
            "alerts_recommended": report.get(
                "alerts_recommended", 0
            )
        },
        "shipment_data": shipment_inputs,
        "records_truncated": len(shipments) > MAX_AI_SHIPMENTS
    }

    return payload, reference_map


def validate_ai_output(output, valid_references):

    if not isinstance(output, dict):
        raise ValueError("AI output must be a JSON object.")

    summary = output.get("executive_summary")
    findings = output.get("priority_findings")
    recommendations = output.get("overall_recommendations")
    limitations = output.get("limitations", [])

    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Invalid executive summary.")

    if not isinstance(findings, list):
        raise ValueError("Invalid priority findings.")

    if not isinstance(recommendations, list):
        raise ValueError("Invalid recommendations.")

    if not isinstance(limitations, list):
        raise ValueError("Invalid limitations.")

    if len(findings) > MAX_AI_SHIPMENTS:
        raise ValueError("Too many AI findings.")

    if len(recommendations) > 20:
        raise ValueError("Too many recommendations.")

    if len(limitations) > 20:
        raise ValueError("Too many limitations.")

    validated_findings = []

    for item in findings:

        if not isinstance(item, dict):
            raise ValueError("Invalid finding format.")

        reference = item.get("reference")

        if reference not in valid_references:
            raise ValueError("Unknown shipment reference.")

        finding = item.get("finding")
        action = item.get("recommended_action")

        if not isinstance(finding, str) or not finding.strip():
            raise ValueError("Invalid finding description.")

        if not isinstance(action, str) or not action.strip():
            raise ValueError("Invalid recommended action.")

        validated_findings.append({
            "reference": reference,
            "finding": finding[:1000],
            "recommended_action": action[:1000]
        })

    for item in recommendations + limitations:
        if not isinstance(item, str):
            raise ValueError("Invalid recommendation format.")

    return {
        "executive_summary": summary[:3000],
        "priority_findings": validated_findings,
        "overall_recommendations": recommendations[:20],
        "limitations": limitations[:20]
    }


def run_openai_reasoning(report):

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    from openai import OpenAI

    model = os.getenv("OPENAI_MODEL", "gpt-5.5")

    payload, reference_map = build_ai_input(report)

    client = OpenAI(
        api_key=api_key,
        timeout=20.0,
        max_retries=1
    )

    try:

        response = client.responses.create(
            model=model,
            instructions=(
                "You are GENRAI-1, the operational reasoning "
                "intelligence of AGS-Pulse. "
                "Analyze shipment monitoring data and identify "
                "operational risks. "
                "Python monitoring rules are authoritative. "
                "Never change, invent, or override shipment data. "
                "Do not claim that a shipment is delayed unless "
                "the supplied monitoring data supports that finding. "
                "Never execute database operations or external actions. "
                "Return JSON only with these keys: "
                "executive_summary, priority_findings, "
                "overall_recommendations, limitations. "
                "Each priority finding must contain reference, "
                "finding, and recommended_action. "
                "Use only shipment references supplied in the input. "
                "If information is insufficient, explicitly say so. "
                "Recommendations are advisory only."
            ),
            input=(
                "Return JSON only. Analyze the following shipment data.\\n\\n"
                + json.dumps(payload)
            ),
            text={
                "format": {
                    "type": "json_object"
                }
            },
            max_output_tokens=1800
        )

        raw_output = response.output_text

        if not raw_output:
            raise ValueError("OpenAI returned an empty response.")

        parsed_output = json.loads(raw_output)

        validated = validate_ai_output(
            parsed_output,
            set(reference_map.keys())
        )

        return {
            "engine": "GENRAI-1",
            "version": VERSION,
            "source": "OPENAI_PRIMARY",
            **validated,
            "database_modified": False,
            "model": model,
            "generated_at": now_utc()
        }, reference_map

    finally:
        client.close()


def execute_genrai_reasoning(report):
    # Local reasoning is the default so GENRAI remains operational without
    # paid API credits. OpenAI is an explicit, opt-in enhancement only.
    _, reference_map = build_ai_input(report)
    use_openai = os.getenv("GENRAI_USE_OPENAI", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }

    if not use_openai:
        local_result = execute_local_rules_engine(report)
        return {
            "success": True,
            "analysis": local_result,
            "shipment_reference_map": reference_map
        }

    try:
        ai_result, reference_map = run_openai_reasoning(report)
        return {
            "success": True,
            "analysis": ai_result,
            "shipment_reference_map": reference_map
        }

    except Exception as exc:
        cause_types = []
        cause = exc
        for _ in range(4):
            cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
            if cause is None:
                break
            cause_types.append(type(cause).__name__)

        # Log only structured, non-secret API error metadata.
        # Never log exception messages, headers, request bodies, or credentials.
        logger.warning(
            "OpenAI reasoning unavailable. Activating local rules engine. "
            "Error type: %s; status: %s; code: %s; param: %s; request_id: %s; cause types: %s",
            type(exc).__name__,
            getattr(exc, "status_code", None),
            getattr(exc, "code", None),
            getattr(exc, "param", None),
            getattr(exc, "request_id", None),
            " -> ".join(cause_types) if cause_types else "not exposed by SDK"
        )

        fallback = execute_local_rules_engine(report)
        fallback["fallback_reason"] = type(exc).__name__

        return {
            "success": True,
            "analysis": fallback,
            "shipment_reference_map": reference_map
        }


# --------------------------------------------------
# ROOT AND HEALTH ENDPOINTS
# --------------------------------------------------

@app.get("/")
def root():

    return {
        "status": "online",
        "application": "AGS-Pulse",
        "engine": "GENRAI-1",
        "version": VERSION,
        "timestamp": now_utc()
    }


@app.get("/ping")
def ping():
    return {"ok": True}


@app.get("/health/db")
def database_health():

    try:

        supabase.table(
            "shipment_telemetry"
        ).select("id").limit(1).execute()

        supabase.table(
            "shipments"
        ).select("id").limit(1).execute()

        return {
            "connected": True,
            "tables": [
                "shipments",
                "shipment_telemetry"
            ],
            "status": "healthy"
        }

    except Exception:

        logger.exception("Database health check failed.")

        raise HTTPException(
            status_code=503,
            detail="Database connection failed."
        )


# --------------------------------------------------
# DASHBOARD AGGREGATE SUMMARY (READ-ONLY)
# --------------------------------------------------

@app.get("/dashboard/summary")
def dashboard_summary(
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default="")
):
    """Return shipment aggregates for the authenticated user's selected organization only."""
    organization_id = resolve_dashboard_organization(
        authorization,
        x_organization_id
    )

    try:
        def tenant_count(table_name):
            result = (
                supabase.table(table_name)
                .select("id", count="exact", head=True)
                .eq("organization_id", organization_id)
                .execute()
            )
            return int(result.count or 0)

        def tenant_event_count(statuses=None):
            query = (
                supabase.table("genrai_telemetry_events")
                .select("event_id", count="exact", head=True)
                .eq("organization_id", organization_id)
            )
            if statuses:
                query = query.in_("status", statuses)
            result = query.execute()
            return int(result.count or 0)

        return {
            "generated_at": now_utc(),
            "scope": "organization",
            "organization_id": organization_id,
            "shipments": {
                "records_total": tenant_count("shipments"),
                "telemetry_records_total": tenant_count("shipment_telemetry")
            },
            "telemetry_events": {
                "available": True,
                "total": tenant_event_count(),
                "analyzed": tenant_event_count(["ANALYZED"]),
                "pending": tenant_event_count(["PENDING_ANALYSIS"]),
                "processing": tenant_event_count(["PROCESSING"]),
                "failed_or_dead_letter": tenant_event_count(["FAILED", "DEAD_LETTER"])
            },
            # Expose only queue-state counts derived from this organization's
            # own telemetry events. Global worker/consumer internals stay private.
            "queue": {
                "available": True,
                "scope": "organization",
                "checked_at": now_utc(),
                "pending": tenant_event_count(["PENDING_ANALYSIS"]),
                "processing": tenant_event_count(["PROCESSING"]),
                "failed": tenant_event_count(["FAILED", "DEAD_LETTER"]),
                "consumers": None,
                "stale_consumers": None,
                "active_alerts": None
            }
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Organization dashboard summary failed.")
        raise HTTPException(
            status_code=503,
            detail="Organization dashboard summary is temporarily unavailable."
        )


# --------------------------------------------------
# --------------------------------------------------
# ORGANIZATION-SCOPED GENRAI EVENT FEED (READ-ONLY)
# --------------------------------------------------

@app.get("/genrai/events")
def list_organization_genrai_events(
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
    limit: int = 50,
    offset: int = 0
):
    """Return recent GENRAI events linked to the verified organization only."""
    organization_id = resolve_dashboard_organization(authorization, x_organization_id)
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100.")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be zero or greater.")
    try:
        result = (
            supabase.table("genrai_telemetry_events")
            .select("event_id,source_system,event_type,status,received_at,analyzed_at,engine_verdict,payload", count="exact")
            .eq("organization_id", organization_id)
            .order("received_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = result.data or []
        shipment_ids = []
        for event in rows:
            event_payload = event.get("payload")
            if not isinstance(event_payload, dict):
                continue
            try:
                shipment_id = int(event_payload.get("shipment_id"))
                shipment_ids.append(shipment_id)
            except (TypeError, ValueError, OverflowError):
                continue

        shipments_by_id = {}
        if shipment_ids:
            tenant_shipments = (
                supabase.table("shipments")
                .select("id,tracking_number,status,origin,destination,carrier,hub_location,status_update,estimated_delivery,updated_at")
                .eq("organization_id", organization_id)
                .in_("id", list(dict.fromkeys(shipment_ids)))
                .execute()
            ).data or []
            shipments_by_id = {shipment.get("id"): shipment for shipment in tenant_shipments}

        # Attach shipment context only after a second organization-scoped
        # lookup. Event payload references alone never authorize shipment data.
        for event in rows:
            event_payload = event.get("payload")
            shipment = None
            if isinstance(event_payload, dict):
                try:
                    shipment = shipments_by_id.get(int(event_payload.get("shipment_id")))
                except (TypeError, ValueError, OverflowError):
                    pass
            event["shipment"] = shipment

        total = int(result.count or 0)
        return {
            "success": True,
            "scope": "organization",
            "organization_id": organization_id,
            "items": rows,
            "pagination": {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(rows) < total}
        }
    except Exception:
        logger.exception("Organization GENRAI event feed failed.")
        raise HTTPException(status_code=503, detail="Organization GENRAI event feed is temporarily unavailable.")

# --------------------------------------------------
# ORGANIZATION-SCOPED INTERNATIONAL ROUTE ASSESSMENTS
# --------------------------------------------------

@app.get("/genrai/routes/assessments")
def list_organization_route_assessments(
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
    limit: int = 50
):
    """Return assessments only for route mappings owned by the verified organization."""
    organization_id = resolve_dashboard_organization(authorization, x_organization_id)
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100.")
    try:
        mappings = (
            supabase.table("genrai_intl_shipment_route_mappings_api")
            .select("route_id,shipment_id,leg_sequence,origin_country,destination_country,transport_mode,provider,active")
            .eq("organization_id", organization_id)
            .eq("active", True)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        ).data or []
        # Enforce tenant ownership on both sides of the mapping. A stale or
        # incorrectly assigned route mapping must never expose another
        # organization's shipment or assessment.
        mapped_shipment_ids = list(dict.fromkeys(
            row.get("shipment_id") for row in mappings if row.get("shipment_id") is not None
        ))
        tenant_shipment_ids = set()
        if mapped_shipment_ids:
            tenant_shipments = (
                supabase.table("shipments")
                .select("id,tracking_number")
                .eq("organization_id", organization_id)
                .in_("id", mapped_shipment_ids)
                .execute()
            ).data or []
            tenant_shipments_by_id = {
                row.get("id"): row for row in tenant_shipments if row.get("id") is not None
            }
        else:
            tenant_shipments_by_id = {}
        mappings = [
            row for row in mappings
            if row.get("shipment_id") in tenant_shipments_by_id
        ]
        route_ids = list(dict.fromkeys(row.get("route_id") for row in mappings if row.get("route_id")))
        assessments = []
        if route_ids:
            assessments = (
                supabase.table("genrai_intl_route_assessments_api")
                .select("route_id,shipment_reference,total_legs,sequenced_legs,sequence_gaps,route_risk_score,risk_band,findings,route_summary,source_event_count,assessed_at")
                .in_("route_id", route_ids)
                .order("assessed_at", desc=True)
                .execute()
            ).data or []
        assessment_by_route = {row.get("route_id"): row for row in assessments}
        items = []
        for mapping in mappings:
            assessment = assessment_by_route.get(mapping.get("route_id"))
            shipment = tenant_shipments_by_id.get(mapping.get("shipment_id"))
            # Assessments are accepted only when their shipment reference
            # matches the verified tenant-owned shipment behind this mapping.
            if (
                assessment
                and shipment
                and clean(assessment.get("shipment_reference")) == clean(shipment.get("tracking_number"))
            ):
                items.append({"mapping": mapping, "assessment": assessment})
        return {"success": True, "scope": "organization", "organization_id": organization_id, "items": items, "total": len(items)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Organization route assessment feed failed.")
        raise HTTPException(status_code=503, detail="Organization route assessments are temporarily unavailable.")

# --------------------------------------------------
# --------------------------------------------------
# ORGANIZATION MEMBERSHIP AND INVITATIONS
# --------------------------------------------------

def authenticated_user_from_header(authorization):
    scheme, separator, token = clean(authorization).partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="A valid Bearer access token is required.")
    try:
        response = supabase.auth.get_user(token.strip())
        user = getattr(response, "user", None)
        user_id = clean(getattr(user, "id", ""))
        if not user_id:
            raise ValueError("Missing authenticated user.")
        return user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")


def organization_member_role(organization_id, user_id):
    rows = (
        supabase.table("organization_memberships")
        .select("member_role")
        .eq("organization_id", organization_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data or []
    return clean(rows[0].get("member_role")) if rows else ""


@app.get("/organizations/{organization_id}/members")
def list_organization_members(
    organization_id: str,
    authorization: str = Header(default="")
):
    """List membership records only for an organization the caller belongs to."""
    user = authenticated_user_from_header(authorization)
    caller_id = clean(getattr(user, "id", ""))
    try:
        caller_memberships = (
            supabase.table("organization_memberships")
            .select("organization_id")
            .eq("user_id", caller_id)
            .execute()
        ).data or []
        if organization_id not in [
            clean(row.get("organization_id")) for row in caller_memberships
        ]:
            raise HTTPException(status_code=403, detail="You are not a member of this organization.")
        rows = (
            supabase.table("organization_memberships")
            .select("organization_id,user_id,member_role,created_at")
            .eq("organization_id", organization_id)
            .order("created_at")
            .execute()
        ).data or []
        return {"success": True, "organization_id": organization_id, "members": rows, "total": len(rows)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Organization member listing failed.")
        raise HTTPException(status_code=503, detail="Organization members are temporarily unavailable.")


@app.post("/organizations/{organization_id}/invitations", status_code=201)
async def create_organization_invitation(
    organization_id: str,
    request: Request,
    authorization: str = Header(default="")
):
    """Invite a verified email into an organization without granting membership before acceptance."""
    user = authenticated_user_from_header(authorization)
    caller_id = clean(getattr(user, "id", ""))
    try:
        caller_role = organization_member_role(organization_id, caller_id)
    except Exception:
        logger.exception("Invitation authorization lookup failed.")
        raise HTTPException(status_code=503, detail="Organization authorization is temporarily unavailable.")
    if caller_role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only organization owners and admins can invite members.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="A valid JSON request body is required.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="A JSON object is required.")

    email = clean(payload.get("email")).casefold()
    role = clean(payload.get("member_role") or "member").casefold()
    if len(email) > 320 or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="A valid email address is required.")
    if role not in {"admin", "dispatcher", "member", "viewer"}:
        raise HTTPException(status_code=422, detail="member_role must be admin, dispatcher, member, or viewer.")
    if role == "admin" and caller_role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner may invite an administrator.")

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    invitation_id = None
    try:
        existing = (
            supabase.table("organization_invitations")
            .select("id")
            .eq("organization_id", organization_id)
            .eq("email", email)
            .eq("status", "pending")
            .limit(1)
            .execute()
        ).data or []
        if existing:
            raise HTTPException(status_code=409, detail="A pending invitation already exists for this email.")

        inserted = supabase.table("organization_invitations").insert({
            "organization_id": organization_id,
            "email": email,
            "member_role": role,
            "token_hash": token_hash,
            "invited_by": caller_id,
            "status": "pending"
        }).execute()
        inserted_rows = inserted.data or []
        if not inserted_rows:
            raise RuntimeError("Invitation record was not created.")
        invitation_id = inserted_rows[0].get("id")

        redirect_to = os.getenv("SUPABASE_INVITE_REDIRECT_URL", "").strip() or None
        options = {"data": {"ags_invitation_token": token}}
        if redirect_to:
            options["redirect_to"] = redirect_to
        supabase.auth.admin.invite_user_by_email(email, options=options)

        return {
            "success": True,
            "invitation_id": invitation_id,
            "organization_id": organization_id,
            "email": email,
            "member_role": role,
            "status": "pending",
            "expires_in_days": 7
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Organization invitation delivery failed.")
        if invitation_id:
            try:
                supabase.table("organization_invitations").delete().eq("id", invitation_id).execute()
            except Exception:
                logger.exception("Failed to clean up undelivered organization invitation.")
        raise HTTPException(status_code=503, detail="Invitation could not be sent. No membership was granted.")


@app.patch("/organizations/{organization_id}/members/{target_user_id}")
async def update_organization_member_role(
    organization_id: str,
    target_user_id: str,
    request: Request,
    authorization: str = Header(default="")
):
    """Change a member role with owner/admin authorization and last-owner safeguards."""
    user = authenticated_user_from_header(authorization)
    actor_id = clean(getattr(user, "id", ""))
    actor_role = organization_member_role(organization_id, actor_id)
    if actor_role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only organization owners and admins can change member roles.")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="A valid JSON request body is required.")
    new_role = clean(payload.get("member_role")).casefold() if isinstance(payload, dict) else ""
    if new_role not in {"owner", "admin", "dispatcher", "member", "viewer"}:
        raise HTTPException(status_code=422, detail="A valid member_role is required.")
    if actor_role != "owner" and new_role in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only an owner may assign owner or admin roles.")
    try:
        result = supabase.rpc("change_organization_member_role", {
            "p_organization_id": organization_id,
            "p_actor_user_id": actor_id,
            "p_target_user_id": target_user_id,
            "p_new_role": new_role
        }).execute()
        data = result.data
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("Role update did not return confirmation.")
        return data
    except HTTPException:
        raise
    except Exception:
        logger.exception("Organization member role update rejected or unavailable.")
        raise HTTPException(status_code=409, detail="Role change was rejected or could not be completed.")


@app.delete("/organizations/{organization_id}/members/{target_user_id}")
def remove_organization_member(
    organization_id: str,
    target_user_id: str,
    authorization: str = Header(default="")
):
    """Remove a member with owner/admin authorization and last-owner safeguards."""
    user = authenticated_user_from_header(authorization)
    actor_id = clean(getattr(user, "id", ""))
    actor_role = organization_member_role(organization_id, actor_id)
    if actor_role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only organization owners and admins can remove members.")
    try:
        result = supabase.rpc("remove_organization_member", {
            "p_organization_id": organization_id,
            "p_actor_user_id": actor_id,
            "p_target_user_id": target_user_id
        }).execute()
        data = result.data
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("Member removal did not return confirmation.")
        return data
    except Exception:
        logger.exception("Organization member removal rejected or unavailable.")
        raise HTTPException(status_code=409, detail="Member removal was rejected or could not be completed.")


@app.post("/organization-invitations/accept")
async def accept_organization_invitation(
    request: Request,
    authorization: str = Header(default="")
):
    """Accept a single-use invitation only for the authenticated, verified email."""
    user = authenticated_user_from_header(authorization)
    user_id = clean(getattr(user, "id", ""))
    email = clean(getattr(user, "email", "")).casefold()
    verified_at = (
        getattr(user, "email_confirmed_at", None)
        or getattr(user, "confirmed_at", None)
    )
    if not email or not verified_at:
        raise HTTPException(status_code=403, detail="A verified email address is required to accept an invitation.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="A valid JSON request body is required.")
    invitation_token = clean(payload.get("invitation_token")) if isinstance(payload, dict) else ""
    if len(invitation_token) < 32 or len(invitation_token) > 256:
        raise HTTPException(status_code=422, detail="A valid invitation token is required.")

    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
    try:
        result = supabase.rpc("accept_organization_invitation", {
            "p_token_hash": token_hash,
            "p_user_id": user_id,
            "p_verified_email": email
        }).execute()
        data = result.data
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("Invitation acceptance did not return confirmation.")
        return data
    except Exception:
        logger.info("Organization invitation acceptance rejected or unavailable for user_id=%s", user_id)
        raise HTTPException(status_code=400, detail="Invitation is invalid, expired, already used, or does not match this account.")


# ORGANIZATION-SCOPED SHIPMENT LIST (READ-ONLY)
# --------------------------------------------------

@app.get("/shipments")
def list_organization_shipments(
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
    limit: int = 25,
    offset: int = 0
):
    """Return a paginated shipment list for a verified organization member."""
    organization_id = resolve_dashboard_organization(authorization, x_organization_id)
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100.")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be zero or greater.")
    try:
        result = (
            supabase.table("shipments")
            .select("id,tracking_number,status,origin,destination,carrier,hub_location,status_update,estimated_delivery,updated_at,production_telemetry_id", count="exact")
            .eq("organization_id", organization_id)
            .order("updated_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = result.data or []
        total = int(result.count or 0)
        return {
            "success": True,
            "scope": "organization",
            "organization_id": organization_id,
            "items": rows,
            "pagination": {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(rows) < total}
        }
    except Exception:
        logger.exception("Organization shipment list failed.")
        raise HTTPException(status_code=503, detail="Organization shipment list is temporarily unavailable.")

# ORGANIZATION-SCOPED MANUAL SHIPMENT CHECKPOINTS
# --------------------------------------------------

@app.get("/shipment-checkpoints")
def list_shipment_checkpoints(
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
    shipment_id: int = 0,
    limit: int = 100
):
    """List real checkpoint history for shipments owned by the verified organization."""
    organization_id = resolve_dashboard_organization(authorization, x_organization_id)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200.")
    try:
        query = (
            supabase.table("shipment_checkpoints")
            .select("id,shipment_id,organization_id,checkpoint_time,location,status,message,source,created_by,created_at")
            .eq("organization_id", organization_id)
        )
        if shipment_id:
            query = query.eq("shipment_id", shipment_id)
        result = query.order("checkpoint_time", desc=True).limit(limit).execute()
        return {
            "success": True,
            "scope": "organization",
            "organization_id": organization_id,
            "items": result.data or []
        }
    except Exception:
        logger.exception("Organization shipment checkpoint list failed.")
        raise HTTPException(status_code=503, detail="Shipment checkpoint history is temporarily unavailable.")


@app.post("/shipment-checkpoints", status_code=201)
async def create_shipment_checkpoint(
    request: Request,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default="")
):
    """Create a manual checkpoint only after verifying user role and shipment ownership."""
    organization_id = resolve_dashboard_organization(authorization, x_organization_id)
    scheme, separator, token = clean(authorization).partition(" ")
    if not separator or scheme.casefold() != "bearer":
        raise HTTPException(status_code=401, detail="A valid Bearer access token is required.")
    try:
        auth_response = supabase.auth.get_user(token.strip())
        user_id = clean(getattr(getattr(auth_response, "user", None), "id", ""))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid or expired access token.")
        membership = (
            supabase.table("organization_memberships")
            .select("member_role")
            .eq("organization_id", organization_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        ).data or []
        role = clean(membership[0].get("member_role")) if membership else ""
        if role not in {"owner", "admin", "dispatcher", "member"}:
            raise HTTPException(status_code=403, detail="Your organization role cannot create checkpoints.")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Checkpoint authorization failed.")
        raise HTTPException(status_code=503, detail="Checkpoint authorization is temporarily unavailable.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="A valid JSON request body is required.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="A JSON object is required.")

    try:
        shipment_id = int(payload.get("shipment_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="shipment_id must be a valid shipment ID.")
    location = clean(payload.get("location"))[:250]
    status = clean(payload.get("status"))[:100]
    message = clean(payload.get("message"))[:2000] or None
    checkpoint_time = clean(payload.get("checkpoint_time")) or now_utc()
    if not location or not status:
        raise HTTPException(status_code=422, detail="location and status are required.")
    try:
        shipment = (
            supabase.table("shipments")
            .select("id")
            .eq("id", shipment_id)
            .eq("organization_id", organization_id)
            .limit(1)
            .execute()
        ).data or []
        if not shipment:
            raise HTTPException(status_code=404, detail="Shipment not found in the selected organization.")
        result = (
            supabase.table("shipment_checkpoints")
            .insert({
                "shipment_id": shipment_id,
                "organization_id": organization_id,
                "checkpoint_time": checkpoint_time,
                "location": location,
                "status": status,
                "message": message,
                "source": "manual",
                "created_by": user_id
            })
            .execute()
        )
        rows = result.data or []
        if not rows:
            raise RuntimeError("Checkpoint insert returned no row.")
        return {"success": True, "scope": "organization", "organization_id": organization_id, "checkpoint": rows[0]}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Organization shipment checkpoint creation failed.")
        raise HTTPException(status_code=503, detail="Checkpoint could not be saved.")


# ORGANIZATION-SCOPED SHIPMENT CREATION
# --------------------------------------------------

@app.post("/shipments", status_code=201)
async def create_organization_shipment(
    request: Request,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default="")
):
    """Create a shipment and its initial telemetry under a verified organization."""
    organization_id = resolve_dashboard_organization(
        authorization,
        x_organization_id
    )

    # Shipment creation is a write operation: viewers may read their tenant's
    # data but only operational roles may create shipments.
    scheme, separator, token = clean(authorization).partition(" ")
    if not separator or scheme.casefold() != "bearer":
        raise HTTPException(status_code=401, detail="A valid Bearer access token is required.")
    try:
        auth_response = supabase.auth.get_user(token.strip())
        user_id = clean(getattr(getattr(auth_response, "user", None), "id", ""))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid or expired access token.")
        membership = (
            supabase.table("organization_memberships")
            .select("member_role")
            .eq("organization_id", organization_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        ).data or []
        role = clean(membership[0].get("member_role")) if membership else ""
        if role not in {"owner", "admin", "dispatcher", "member"}:
            raise HTTPException(status_code=403, detail="Your organization role cannot create shipments.")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Shipment creation authorization failed.")
        raise HTTPException(status_code=503, detail="Shipment authorization is temporarily unavailable.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="A valid JSON request body is required.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="A JSON object is required.")

    tracking_number = clean(payload.get("tracking_number"))[:120]
    origin = clean(payload.get("origin"))[:250]
    destination = clean(payload.get("destination"))[:250]
    carrier = clean(payload.get("carrier"))[:100]
    status = clean(payload.get("status"))[:100] or "Pending"

    if not tracking_number or not origin or not destination:
        raise HTTPException(
            status_code=422,
            detail="tracking_number, origin, and destination are required."
        )

    shipment_id = None
    telemetry_id = None
    try:
        # The organization ID is derived only from the verified membership.
        # Any organization_id sent in the request body is intentionally ignored.
        shipment_result = (
            supabase.table("shipments")
            .insert({
                "tracking_number": tracking_number,
                "status": status,
                "origin": origin,
                "destination": destination,
                "carrier": carrier or None,
                "organization_id": organization_id,
                "updated_at": now_utc()
            })
            .execute()
        )
        created_shipments = shipment_result.data or []
        if not created_shipments:
            raise RuntimeError("Shipment insert returned no row.")
        shipment = created_shipments[0]
        shipment_id = shipment["id"]

        # The database trigger copies organization_id from the parent shipment.
        telemetry_result = (
            supabase.table("shipment_telemetry")
            .insert({
                "shipment_id": shipment_id,
                "rfq_id": tracking_number,
                "origin": origin,
                "destination": destination,
                "carrier": carrier or None,
                "status": status,
                "status_update": "Shipment created in AGS-Pulse.",
                "delay_flag": False,
                "dwell_hours": 0
            })
            .execute()
        )
        telemetry_rows = telemetry_result.data or []
        if not telemetry_rows:
            raise RuntimeError("Initial telemetry insert returned no row.")
        telemetry_id = telemetry_rows[0]["id"]

        linked = (
            supabase.table("shipments")
            .update({"production_telemetry_id": telemetry_id})
            .eq("id", shipment_id)
            .eq("organization_id", organization_id)
            .execute()
        )
        if not linked.data:
            raise RuntimeError("Initial telemetry could not be linked to the shipment.")

        return {
            "success": True,
            "scope": "organization",
            "organization_id": organization_id,
            "shipment": {
                "id": shipment_id,
                "tracking_number": tracking_number,
                "status": status,
                "origin": origin,
                "destination": destination,
                "carrier": carrier or None
            },
            "telemetry_id": telemetry_id
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Organization-scoped shipment creation failed.")
        if shipment_id is not None:
            try:
                # Remove any child row before the parent to respect the FK.
                if telemetry_id is not None:
                    supabase.table("shipment_telemetry").delete().eq("id", telemetry_id).eq(
                        "shipment_id", shipment_id
                    ).execute()
                supabase.table("shipments").delete().eq("id", shipment_id).eq(
                    "organization_id", organization_id
                ).execute()
            except Exception:
                logger.exception("Shipment creation rollback failed.")
        raise HTTPException(
            status_code=503,
            detail="Shipment could not be created. No successful creation was confirmed."
        )


# --------------------------------------------------
# AUTHENTICATED ORGANIZATION-SCOPED TRACKING UPDATE
# --------------------------------------------------

@app.post("/shipments/{shipment_id}/tracking-update")
async def update_organization_shipment_tracking(
    shipment_id: int,
    request: Request,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default="")
):
    """Atomically update a tenant shipment, emit telemetry, and append its checkpoint."""
    organization_id = resolve_dashboard_organization(authorization, x_organization_id)

    scheme, separator, token = clean(authorization).partition(" ")
    if not separator or scheme.casefold() != "bearer":
        raise HTTPException(status_code=401, detail="A valid Bearer access token is required.")
    try:
        auth_response = supabase.auth.get_user(token.strip())
        user_id = clean(getattr(getattr(auth_response, "user", None), "id", ""))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="A valid JSON request body is required.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="A JSON object is required.")

    status = clean(payload.get("status"))[:100]
    location = clean(payload.get("location"))[:250]
    status_update = clean(payload.get("status_update"))[:2000] or "Manual tracking update recorded in AGS-Pulse."
    message = clean(payload.get("message"))[:2000] or None
    raw_checkpoint_time = clean(payload.get("checkpoint_time"))

    if not status or not location:
        raise HTTPException(status_code=422, detail="status and location are required.")

    checkpoint_time = now_utc()
    if raw_checkpoint_time:
        try:
            parsed_time = datetime.fromisoformat(raw_checkpoint_time.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                parsed_time = parsed_time.replace(tzinfo=timezone.utc)
            checkpoint_time = parsed_time.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise HTTPException(status_code=422, detail="checkpoint_time must be a valid ISO 8601 timestamp.")

    try:
        result = supabase.rpc(
            "record_org_shipment_tracking_update",
            {
                "p_organization_id": organization_id,
                "p_shipment_id": shipment_id,
                "p_user_id": user_id,
                "p_status": status,
                "p_location": location,
                "p_status_update": status_update,
                "p_checkpoint_time": checkpoint_time,
                "p_message": message
            }
        ).execute()
        if not result.data:
            raise RuntimeError("Tracking update returned no confirmation.")
        return {
            "success": True,
            "scope": "organization",
            "organization_id": organization_id,
            "tracking_update": result.data
        }
    except HTTPException:
        raise
    except Exception as exc:
        error_text = str(exc).casefold()
        logger.exception("Organization shipment tracking update failed.")
        if "shipment not found in selected organization" in error_text:
            raise HTTPException(status_code=404, detail="Shipment not found in the selected organization.")
        if "cannot update shipments in this organization" in error_text:
            raise HTTPException(status_code=403, detail="Your organization role cannot update shipments.")
        if "status and location are required" in error_text:
            raise HTTPException(status_code=422, detail="status and location are required.")
        raise HTTPException(status_code=503, detail="Tracking update could not be saved.")

 
# --------------------------------------------------
# PROTECTED DATABASE TEST ENDPOINT
# --------------------------------------------------

@app.get("/db-test")
def db_test(
    x_genrai_token: str = Header(default="")
):

    authorize_genrai(x_genrai_token)

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
        "version": VERSION,
        "status": "api_online",
        "openai_configured": bool(
            os.getenv("OPENAI_API_KEY")
        ),
        "openai_model": os.getenv(
            "OPENAI_MODEL", "gpt-5.5"
        ),
        "fallback_available": True,
        "active_background_loops": False,
        "monitoring_enabled": True,
        "monitoring_mode": "on_demand_safe_read_only",
        "dwell_threshold_hours": DWELL_TIME_THRESHOLD_HOURS,
        "database_writes_during_monitoring": False
    }


# --------------------------------------------------
# EXISTING TELEMETRY SYNCHRONIZATION
# --------------------------------------------------

def execute_genrai_telemetry_sync():

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

        raise RuntimeError(
            "Could not retrieve shipments."
        ) from exc

    for shipment in shipments:

        stats["shipments_checked"] += 1

        try:

            shipment_id = shipment.get("id")

            tracking_number = clean(
                shipment.get("tracking_number")
            )

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

            linked_result = (
                supabase.table("shipment_telemetry")
                .select("*")
                .eq("shipment_id", shipment_id)
                .execute()
            )

            linked_rows = linked_result.data or []

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

                    if (
                        same_value(
                            row.get("rfq_id"),
                            tracking_number
                        )
                        and route_matches(
                            row,
                            origin,
                            destination,
                            require_complete=True
                        )
                    ):

                        (
                            supabase.table("shipment_telemetry")
                            .update({
                                "shipment_id": shipment_id
                            })
                            .eq("id", row["id"])
                            .is_("shipment_id", "null")
                            .execute()
                        )

                        stats["existing_records_linked"] += 1

                linked_result = (
                    supabase.table("shipment_telemetry")
                    .select("*")
                    .eq("shipment_id", shipment_id)
                    .execute()
                )

                linked_rows = linked_result.data or []

            if not linked_rows:

                inserted = (
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
                    .select("id")
                    .execute()
                )
                inserted_rows = inserted.data or []
                if len(inserted_rows) == 1:
                    (
                        supabase.table("shipments")
                        .update({"production_telemetry_id": inserted_rows[0]["id"]})
                        .eq("id", shipment_id)
                        .is_("production_telemetry_id", "null")
                        .execute()
                    )

                stats["records_created"] += 1
                continue

            latest = resolve_production_telemetry_row(
                shipment,
                linked_rows
            )

            if latest is None:
                logger.warning(
                    "Skipped shipment %s: no explicit or uniquely identifiable production telemetry row.",
                    shipment_id
                )
                stats["shipments_skipped"] += 1
                continue

            if shipment.get("production_telemetry_id") is None:
                (
                    supabase.table("shipments")
                    .update({"production_telemetry_id": latest["id"]})
                    .eq("id", shipment_id)
                    .is_("production_telemetry_id", "null")
                    .execute()
                )

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

    logger.info(
        "GENRAI-1 sync completed: checked=%s created=%s "
        "updated=%s linked=%s skipped=%s errors=%s",
        stats["shipments_checked"],
        stats["records_created"],
        stats["records_updated"],
        stats["existing_records_linked"],
        stats["shipments_skipped"],
        len(stats["errors"])
    )

    if stats["success"]:
        record_genrai_progress(
            "genrai-telemetry-sync-cycle",
            "GENRAI telemetry synchronization",
            "Latest automated shipment-to-telemetry synchronization result.",
            evidence={key: stats[key] for key in ("shipments_checked", "records_created", "records_updated", "existing_records_linked", "shipments_skipped", "completed_at")},
            verification_result="Synchronization completed without row-level errors."
        )

    return stats


@app.post("/genrai/run-telemetry-sync")
def run_telemetry_sync(
    x_genrai_token: str = Header(default="")
):

    authorize_genrai(x_genrai_token)

    try:

        results = execute_genrai_telemetry_sync()

        return {
            "message": (
                f"GENRAI-1 v{VERSION} synchronization completed."
            ),
            "results": results
        }

    except Exception:

        logger.exception("GENRAI-1 synchronization failed.")

        raise HTTPException(
            status_code=500,
            detail="Synchronization failed. Check Render logs."
        )


# --------------------------------------------------
# AUTONOMOUS MONITORING ENGINE
# SAFE MODE: READ ONLY
# --------------------------------------------------

def execute_genrai_autonomous_monitor():

    current_time = datetime.now(timezone.utc)

    report = {
        "engine": "GENRAI-1",
        "version": VERSION,
        "mode": "SAFE_TEST_READ_ONLY",
        "monitoring_started": current_time.isoformat(),
        "shipments_checked": 0,
        "clear_shipments": 0,
        "unknown_shipments": 0,
        "excluded_test_shipments": 0,
        "alerts_recommended": 0,
        "errors": [],
        "shipments": [],
        "database_modified": False
    }

    try:

        shipment_result = (
            supabase.table("shipments")
            .select(
                "id,tracking_number,status,origin,destination,updated_at,"
                "production_telemetry_id"
            )
            .execute()
        )

        telemetry_result = (
            supabase.table("shipment_telemetry")
            .select(
                "id,shipment_id,rfq_id,hub_location,"
                "status,carrier,status_update,estimated_delivery,"
                "delay_flag,dwell_hours,delay_reason,"
                "last_updated,last_autonomous_check,"
                "created_at"
            )
            .execute()
        )

        shipments = shipment_result.data or []
        telemetry_rows = telemetry_result.data or []

    except Exception as exc:

        logger.exception(
            "GENRAI-1 monitoring data retrieval failed."
        )

        raise RuntimeError(
            "Unable to retrieve monitoring data."
        ) from exc

    telemetry_by_shipment = {}

    for row in telemetry_rows:

        shipment_id = row.get("shipment_id")

        if shipment_id is None:
            continue

        telemetry_by_shipment.setdefault(
            str(shipment_id), []
        ).append(row)

    for shipment in shipments:

        report["shipments_checked"] += 1

        shipment_id = shipment.get("id")

        result = {
            "shipment_id": shipment_id,
            "tracking_number": shipment.get("tracking_number"),
            "route": {
                "origin": shipment.get("origin"),
                "destination": shipment.get("destination")
            },
            "master_status": shipment.get("status"),
            "monitoring_state": "UNKNOWN",
            "missing_information": [],
            "recorded_dwell_hours": None,
            "dwell_threshold_hours": DWELL_TIME_THRESHOLD_HOURS,
            "dwell_threshold_exceeded": False,
            "existing_delay_flag": False,
            "overdue_estimated_delivery": None,
            "delay_reason": None,
            "alert_recommended": False
        }

        try:

            linked_rows = telemetry_by_shipment.get(
                str(shipment_id), []
            )

            telemetry = resolve_production_telemetry_row(
                shipment,
                linked_rows
            )

            if telemetry is None:
                production_id = shipment.get("production_telemetry_id")
                linked_test_row = next((
                    row for row in linked_rows
                    if production_id is not None
                    and clean(row.get("id")) == clean(production_id)
                    and clean(row.get("shipment_id")) == clean(shipment_id)
                    and is_test_telemetry_row(row)
                ), None)

                if linked_test_row is not None:
                    result["monitoring_state"] = "EXCLUDED_TEST_DATA"
                    result["missing_information"].append(
                        "Linked telemetry is marked as test or simulated data"
                    )
                    report["excluded_test_shipments"] += 1
                else:
                    result["missing_information"].append(
                        "No explicit or uniquely identifiable production telemetry record"
                    )
                    report["unknown_shipments"] += 1

                report["shipments"].append(result)
                continue

            required_fields = {
                "hub_location": telemetry.get("hub_location"),
                "status_update": telemetry.get("status_update"),
                "estimated_delivery": telemetry.get(
                    "estimated_delivery"
                ),
                "last_updated": telemetry.get("last_updated")
            }

            for field, value in required_fields.items():

                if not clean(value):
                    result["missing_information"].append(field)

            dwell_hours = parse_dwell_hours(
                telemetry.get("dwell_hours")
            )

            result["recorded_dwell_hours"] = dwell_hours

            if dwell_hours is not None:
                result["dwell_threshold_exceeded"] = (
                    dwell_hours > DWELL_TIME_THRESHOLD_HOURS
                )

            result["existing_delay_flag"] = (
                telemetry.get("delay_flag") is True
            )

            result["delay_reason"] = telemetry.get(
                "delay_reason"
            )

            estimated_delivery = telemetry.get(
                "estimated_delivery"
            )

            if clean(estimated_delivery):

                terminal = clean(
                    shipment.get("status")
                ).casefold() in TERMINAL_STATUSES

                if terminal:
                    result["overdue_estimated_delivery"] = False
                else:
                    result["overdue_estimated_delivery"] = (
                        delivery_is_overdue(
                            estimated_delivery,
                            current_time
                        )
                    )

            detection = (
                result["dwell_threshold_exceeded"]
                or result["existing_delay_flag"]
                or result["overdue_estimated_delivery"] is True
            )

            result["alert_recommended"] = detection

            if detection:

                result["monitoring_state"] = "ALERT"
                report["alerts_recommended"] += 1

            elif result["missing_information"]:

                result["monitoring_state"] = "UNKNOWN"
                report["unknown_shipments"] += 1

            else:

                result["monitoring_state"] = "CLEAR"
                report["clear_shipments"] += 1

            report["shipments"].append(result)

        except Exception as exc:

            logger.exception(
                "Monitoring failed for shipment %s.",
                shipment_id
            )

            report["errors"].append({
                "shipment_id": shipment_id,
                "error": str(exc)
            })

            result["monitoring_state"] = "UNKNOWN"
            result["missing_information"].append(
                "Monitoring evaluation failed"
            )

            report["unknown_shipments"] += 1
            report["shipments"].append(result)

    report["monitoring_completed"] = now_utc()

    report["success"] = len(report["errors"]) == 0

    logger.info(
        "GENRAI-1 monitoring completed: checked=%s clear=%s "
        "unknown=%s alerts=%s errors=%s",
        report["shipments_checked"],
        report["clear_shipments"],
        report["unknown_shipments"],
        report["alerts_recommended"],
        len(report["errors"])
    )

    if report["success"]:
        record_genrai_progress(
            "genrai-autonomous-monitor-cycle",
            "GENRAI autonomous monitoring cycle",
            "Latest scheduled safe-mode monitoring cycle and shipment exception counts.",
            evidence={key: report[key] for key in ("shipments_checked", "clear_shipments", "unknown_shipments", "excluded_test_shipments", "alerts_recommended", "monitoring_completed")},
            verification_result="Monitoring cycle completed successfully; source shipment records were not modified."
        )

    return report


@app.post("/genrai/run-autonomous-monitor")
def run_autonomous_monitor(
    x_genrai_token: str = Header(default="")
):

    authorize_genrai(x_genrai_token)

    try:

        results = execute_genrai_autonomous_monitor()

        return {
            "message": (
                f"GENRAI-1 v{VERSION} "
                "safe-mode monitoring completed."
            ),
            "results": results
        }

    except Exception:

        logger.exception(
            "GENRAI-1 autonomous monitoring failed."
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Autonomous monitoring failed. "
                "Check Render logs."
            )
        )


# --------------------------------------------------
# INTELLIGENCE ANALYSIS ENDPOINT
# --------------------------------------------------

@app.post("/genrai/run-intelligence-analysis")
def run_intelligence_analysis(
    x_genrai_token: str = Header(default="")
):

    authorize_genrai(x_genrai_token)

    try:

        report = execute_genrai_autonomous_monitor()

        if not report.get("success"):
            raise HTTPException(
                status_code=503,
                detail="Monitoring report contains errors."
            )

        reasoning = execute_genrai_reasoning(report)
        record_genrai_progress(
            "genrai-intelligence-analysis-cycle",
            "GENRAI intelligence analysis cycle",
            "Latest completed monitoring and intelligence-reasoning cycle.",
            evidence={
                "shipments_checked": report["shipments_checked"],
                "alerts_recommended": report["alerts_recommended"],
                "reasoning_completed": True,
                "completed_at": now_utc()
            },
            verification_result="Monitoring and intelligence reasoning completed successfully."
        )

        return {
            "message": (
                f"GENRAI-1 v{VERSION} "
                "intelligence analysis completed."
            ),
            "monitoring": {
                "shipments_checked": report["shipments_checked"],
                "clear_shipments": report["clear_shipments"],
                "unknown_shipments": report["unknown_shipments"],
                "alerts_recommended": report["alerts_recommended"]
            },
            "reasoning": reasoning,
            "database_modified": False,
            "completed_at": now_utc()
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception(
            "GENRAI-1 intelligence analysis failed."
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Intelligence analysis failed. "
                "Check Render logs."
            )
        )
