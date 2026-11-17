"""
Investment Overview routes
"""

from fastapi import APIRouter
from datetime import datetime, timezone

from ..database import get_db
from ..utils import serialize_objectid

router = APIRouter(prefix="/api/investment-overview")


@router.get("")
async def get_investment_overview():
    db = get_db()
    instruments = await db.market_pulse.find().sort("category", 1).to_list(None)
    instruments = [serialize_objectid(i) for i in instruments]
    return {"instruments": instruments}


@router.get("/meta")
async def get_meta():
    db = get_db()
    meta = await db.market_pulse_meta.find_one({"type": "last_refresh"})
    if meta:
        meta_dict = serialize_objectid(meta)
        return {**meta_dict, "last_updated": meta_dict.get("executed_at") or meta_dict.get("last_updated")}
    # Fall back to seeded_at from most recent instrument
    first = await db.market_pulse.find_one(
        {}, sort=[("seeded_at", -1)]
    )
    if first:
        return {
            "last_updated": first.get("seeded_at"),
            "time_taken_sec": None,
            "status": "seeded"
        }
    return {"last_updated": None, "time_taken_sec": None, "status": "none"}


@router.post("/refresh")
async def refresh_investment_data():
    db = get_db()
    template_doc = await db.query_templates.find_one({"name": "MarketPulse"})
    if not template_doc:
        return {"error": "MarketPulse template not found in query_templates collection."}

    from ..tasks import execute_investment_refresh
    task = execute_investment_refresh.delay(template_doc.get("template", ""))
    return {"task_id": task.id, "status": "scheduled"}


@router.get("/refresh-status/{task_id}")
async def get_refresh_status(task_id: str):
    from ..celery_config import get_celery_app
    app = get_celery_app()
    result = app.AsyncResult(task_id)
    return {"task_id": task_id, "state": result.state, "result": result.result if result.ready() else None}


@router.get("/economy-indicators")
async def get_economy_indicators():
    db = get_db()
    indicators = await db.economy_indicator.find().sort("indicator", 1).to_list(None)
    indicators = [serialize_objectid(i) for i in indicators]
    return {"indicators": indicators}


@router.get("/economy-indicators/meta")
async def get_economy_indicators_meta():
    db = get_db()
    meta = await db.economy_indicator_meta.find_one({"type": "last_refresh"})
    if meta:
        meta_dict = serialize_objectid(meta)
        return {**meta_dict, "last_updated": meta_dict.get("executed_at") or meta_dict.get("last_updated")}
    return {"last_updated": None, "status": "none"}


@router.post("/economy-indicators/refresh")
async def refresh_economy_indicators():
    db = get_db()
    template_doc = await db.query_templates.find_one({"name": "EconomyIndicator"})
    if not template_doc:
        return {"error": "EconomyIndicator template not found in query_templates collection."}

    from ..tasks import execute_economy_indicator_refresh
    task = execute_economy_indicator_refresh.delay(template_doc.get("template", ""))
    return {"task_id": task.id, "status": "scheduled"}


@router.get("/economy-indicators/refresh-status/{task_id}")
async def get_economy_indicators_refresh_status(task_id: str):
    from ..celery_config import get_celery_app
    app = get_celery_app()
    result = app.AsyncResult(task_id)
    return {"task_id": task_id, "state": result.state, "result": result.result if result.ready() else None}
