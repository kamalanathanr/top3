"""
Flight plan routes — the Trips page's "Find Flights" button.

Pricing is scraped from a real browser one leg at a time, so this never does
the work in the request: the POST queues a Celery task and the page polls the
GET for progress and, eventually, the ranked itineraries.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..database import get_db
from ..celery_config import get_celery_app
from ..utils import serialize_objectid

router = APIRouter(prefix="/api/flight-plan")

USER_ID = "Kamal"


@router.get("")
async def get_flight_plan():
    """Latest search for this user: status, streamed progress, and the plan."""
    db = get_db()
    doc = await db.flight_plans.find_one({"user_id": USER_ID})
    if not doc:
        return {"status": "none"}
    return serialize_objectid(doc)


@router.post("")
async def start_flight_plan():
    """Queue a new search, unless one is already in flight."""
    db = get_db()
    doc = await db.flight_plans.find_one({"user_id": USER_ID})
    if doc and doc.get("status") in ("queued", "running"):
        return JSONResponse({
            "status": doc["status"],
            "task_id": doc.get("task_id"),
            "already_running": True,
        })

    details = await db.trip_details.find_one({"user_id": USER_ID}) or {}
    if not (details.get("interested_countries") or []):
        return JSONResponse(
            {"error": "Tick at least one country under \"Countries Interested "
                      "In Visiting\" before searching for flights."},
            status_code=400,
        )

    from src.tasks import execute_flight_plan
    task = execute_flight_plan.delay(USER_ID)

    # Show "queued" straight away rather than leaving the panel blank until
    # the worker picks the task up.
    await db.flight_plans.update_one(
        {"user_id": USER_ID},
        {"$set": {"user_id": USER_ID, "status": "queued", "task_id": task.id,
                  "error": None, "log": [], "plan": None}},
        upsert=True,
    )
    return JSONResponse({"status": "queued", "task_id": task.id})


@router.post("/cancel")
async def cancel_flight_plan():
    """Stop a running search (the browser scrape is killed with it)."""
    db = get_db()
    doc = await db.flight_plans.find_one({"user_id": USER_ID})
    task_id = (doc or {}).get("task_id")
    if task_id:
        get_celery_app().control.revoke(task_id, terminate=True, signal="SIGTERM")
    await db.flight_plans.update_one(
        {"user_id": USER_ID},
        {"$set": {"status": "cancelled"}},
    )
    return {"status": "cancelled"}
