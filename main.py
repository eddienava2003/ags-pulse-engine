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

VERSION = "1.7.2"

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-GENRAI-TOKEN"]
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
        raise HTTPException(
            status_code=500,
            detail="AfterShip webhook secret is not configured."
        )

    body = await request.body()
    received_signature = request.headers.get("aftership-hmac-sha256", "").strip()
    expected_signature = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256
        ).digest()
    ).decode("utf-8")

    if not received_signature or not hmac.compare_digest(
        expected_signature, received_signature
    ):
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
        raise HTTPException(status_code=400, detail="AfterShip msg object is required.")

    tracking_number = clean(msg.get("tracking_number"))
    if not tracking_number:
        raise HTTPException(status_code=400, detail="AfterShip tracking_number is required.")

    # Prefer the provider's event identifier. When absent, hash the exact signed
    # body so retries of the same delivery resolve to the same GENRAI event.
    provider_event_id = clean(
        payload.get("event_id") or msg.get("event_id") or msg.get("id")
    )
    event_fingerprint = hashlib.sha256(body).hexdigest()
    genrai_event_id = (
        "aftership_" + provider_event_id
        if provider_event_id else "aftership_" + event_fingerprint
    )[:200]

    try:
        # Idempotency check MUST precede all shipment/telemetry writes.
        existing = (
            supabase.table("genrai_telemetry_events")
            .select("event_id,status,analyzed_at,engine_verdict,last_processing_error")
            .eq("event_id", genrai_event_id)
            .limit(1)
            .execute()
        ).data or []
        if existing:
            prior = existing[0]
            prior_status = prior.get("status")
            return {
                "success": True,
                "duplicate": True,
                "event_id": genrai_event_id,
                "status": prior_status,
                "analyzed_at": prior.get("analyzed_at"),
                "engine_verdict": prior.get("engine_verdict"),
                "last_processing_error": prior.get("last_processing_error"),
                "processing_pending": prior_status in {"PENDING_ANALYSIS", "PROCESSING"}
            }

        shipment_result = (
            supabase.table("shipments")
            .select("id,tracking_number,status,carrier")
            .eq("tracking_number", tracking_number)
            .limit(1)
            .execute()
        )
        shipments = shipment_result.data or []
        if not shipments:
            logger.warning("AfterShip tracking number is not mapped to an AGS shipment.")
            return {"success": True, "unmatched": True, "tracking_number": tracking_number}

        shipment = shipments[0]
        telemetry_result = (
            supabase.table("shipment_telemetry")
            .select("id,dwell_hours,delay_flag")
            .eq("shipment_id", shipment["id"])
            .order("last_updated", desc=True)
            .limit(1)
            .execute()
        )
        telemetry_rows = telemetry_result.data or []
        if not telemetry_rows:
            raise RuntimeError("Shipment has no linked telemetry row; refusing an unlinked write.")
        telemetry = telemetry_rows[0]
        telemetry_id = telemetry["id"]

        checkpoints = msg.get("checkpoints") or []
        latest_checkpoint = checkpoints[-1] if isinstance(checkpoints, list) and checkpoints else {}
        if not isinstance(latest_checkpoint, dict):
            latest_checkpoint = {}

        operational_status = clean(
            latest_checkpoint.get("subtag_message")
            or msg.get("subtag_message")
            or msg.get("tag")
            or event_name
        )[:200]
        location = clean(
            latest_checkpoint.get("location")
            or latest_checkpoint.get("city")
            or msg.get("destination_city")
        )[:250]
        carrier = clean(msg.get("slug") or shipment.get("carrier"))[:100]
        tag = clean(msg.get("tag") or latest_checkpoint.get("tag"))
        delay_flag = tag.casefold() in {"exception", "failedattempt", "expired"}

        estimated = (
            (msg.get("latest_estimated_delivery") or {}).get("datetime")
            or (msg.get("aftership_estimated_delivery_date") or {}).get("estimated_delivery_date")
            or (msg.get("courier_estimated_delivery_date") or {}).get("estimated_delivery_date")
        )
        if estimated and len(estimated) == 10:
            estimated = estimated + "T00:00:00+00:00"

        checkpoint_message = clean(
            latest_checkpoint.get("message")
            or latest_checkpoint.get("subtag_message")
            or operational_status
        )[:1000]

        event_payload = {
            "id": telemetry_id,
            "shipment_id": shipment["id"],
            "tracking_number": tracking_number,
            "carrier": carrier,
            "event": event_name,
            "event_status": operational_status,
            "location": location,
            "delay_flag": delay_flag,
            "dwell_hours": telemetry.get("dwell_hours"),
            "provider": "AfterShip",
            "provider_event_id": provider_event_id
        }

        telemetry_update = {
            "status": operational_status,
            "hub_location": location or None,
            "status_update": checkpoint_message,
            "carrier": carrier or None,
            "delay_flag": delay_flag,
            "last_updated": now_utc()
        }
        if estimated:
            telemetry_update["estimated_delivery"] = estimated

        supabase.table("shipment_telemetry").update(
            telemetry_update
        ).eq("id", telemetry_id).execute()

        shipment_update = {
            "status": operational_status,
            "hub_location": location or None,
            "status_update": checkpoint_message,
            "carrier": carrier or None,
            "updated_at": now_utc()
        }
        if estimated:
            shipment_update["estimated_delivery"] = estimated
        supabase.table("shipments").update(
            shipment_update
        ).eq("id", shipment["id"]).execute()

        supabase.table("genrai_telemetry_events").insert({
            "event_id": genrai_event_id,
            "source_system": "AfterShip",
            "event_type": "SHIPMENT_TELEMETRY_UPDATE",
            "payload": event_payload,
            "status": "PENDING_ANALYSIS"
        }).execute()

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
        accepted_statuses = {"ANALYZED", "REJECTED", "PENDING_ANALYSIS", "PROCESSING"}

        return {
            # PENDING_ANALYSIS/PROCESSING means accepted and queued, not failed.
            "success": result_status in accepted_statuses,
            "accepted": True,
            "duplicate": False,
            "processing_pending": result_status in {"PENDING_ANALYSIS", "PROCESSING"},
            "event_id": genrai_event_id,
            "status": result_status,
            "analyzed_at": result.get("analyzed_at"),
            "engine_verdict": result.get("engine_verdict"),
            "last_processing_error": result.get("last_processing_error"),
            "shipment_id": shipment["id"],
            "telemetry_id": telemetry_id,
            "shipment_updated": True
        }

    except HTTPException:
        raise
    except Exception:
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

    api_key = os.getenv("OPENAI_API_KEY")

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
            input=json.dumps(payload),
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

    try:

        ai_result, reference_map = run_openai_reasoning(report)

        return {
            "success": True,
            "analysis": ai_result,
            "shipment_reference_map": reference_map
        }

    except Exception as exc:

        logger.warning(
            "OpenAI reasoning unavailable. "
            "Activating local rules engine. Error type: %s",
            type(exc).__name__
        )

        fallback = execute_local_rules_engine(report)

        _, reference_map = build_ai_input(report)

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

            valid_rows = []

            for row in linked_rows:

                row_tracking = clean(row.get("rfq_id"))

                if row_tracking and not same_value(
                    row_tracking, tracking_number
                ):
                    continue

                if not route_matches(
                    row, origin, destination
                ):
                    continue

                valid_rows.append(row)

            if not valid_rows:

                logger.warning(
                    "Skipped shipment %s due to tracking or route mismatch.",
                    shipment_id
                )

                stats["shipments_skipped"] += 1
                continue

            valid_rows.sort(
                key=safe_row_sort_key,
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
        "alerts_recommended": 0,
        "errors": [],
        "shipments": [],
        "database_modified": False
    }

    try:

        shipment_result = (
            supabase.table("shipments")
            .select(
                "id,tracking_number,status,origin,destination,updated_at"
            )
            .execute()
        )

        telemetry_result = (
            supabase.table("shipment_telemetry")
            .select(
                "id,shipment_id,rfq_id,hub_location,"
                "status_update,estimated_delivery,"
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

            valid_rows = []

            for row in linked_rows:

                row_tracking = clean(row.get("rfq_id"))

                tracking_number = clean(
                    shipment.get("tracking_number")
                )

                if row_tracking and not same_value(
                    row_tracking, tracking_number
                ):
                    continue

                if not route_matches(
                    row,
                    shipment.get("origin"),
                    shipment.get("destination")
                ):
                    continue

                valid_rows.append(row)

            if not valid_rows:

                result["missing_information"].append(
                    "No matching telemetry record"
                )

                report["unknown_shipments"] += 1
                report["shipments"].append(result)
                continue

            valid_rows.sort(
                key=safe_row_sort_key,
                reverse=True
            )

            telemetry = valid_rows[0]

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
