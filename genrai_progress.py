import logging
import os
import secrets

from fastapi import APIRouter, Header, HTTPException
from supabase import create_client

logger = logging.getLogger("ags-pulse.progress")
router = APIRouter(prefix="/genrai/project-progress", tags=["GENRAI Project Progress"])


def _authorize(token: str) -> None:
    expected = os.getenv("GENRAI_SYNC_TOKEN", "").strip()
    supplied = (token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="GENRAI authentication token is not configured.")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")


def _client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Supabase configuration is missing.")
    return create_client(url, key)


@router.get("")
def get_project_progress(x_genrai_token: str = Header(default="")):
    """Return GENRAI's persistent project-progress ledger."""
    _authorize(x_genrai_token)
    try:
        result = _client().rpc("genrai_get_project_progress").execute()
        records = result.data or []
        return {
            "success": True,
            "count": len(records),
            "records": records
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("GENRAI project-progress read failed.")
        raise HTTPException(status_code=503, detail="Unable to read project progress.")


@router.post("")
def upsert_project_progress(payload: dict, x_genrai_token: str = Header(default="")):
    """Create or update one project-progress record by task_key."""
    _authorize(x_genrai_token)
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="A JSON progress record is required.")
    for field in ("task_key", "workstream", "title"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise HTTPException(status_code=400, detail=f"{field} is required.")
    if len(payload["task_key"]) > 160 or len(payload["workstream"]) > 100 or len(payload["title"]) > 300:
        raise HTTPException(status_code=400, detail="A progress field exceeds its maximum length.")
    if not isinstance(payload.get("evidence", {}), dict):
        raise HTTPException(status_code=400, detail="evidence must be a JSON object.")
    try:
        result = _client().rpc(
            "genrai_upsert_project_progress",
            {"p_record": payload}
        ).execute()
        return {"success": True, "record": result.data}
    except HTTPException:
        raise
    except Exception:
        logger.exception("GENRAI project-progress write failed: task_key=%s", payload.get("task_key"))
        raise HTTPException(status_code=503, detail="Unable to update project progress.")
