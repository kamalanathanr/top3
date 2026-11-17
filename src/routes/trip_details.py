"""
Trip details routes — persists everything chosen/typed in the Trips page's
"Trip Details" expand/collapse section (travellers, dates, budget) to the
`trip_details` MongoDB collection.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Body

from ..database import get_db
from ..utils import serialize_objectid

router = APIRouter(prefix="/api/trip-details")


@router.get("")
async def get_trip_details():
    db = get_db()
    details = await db.trip_details.find_one({"user_id": "Kamal"})
    if not details:
        return {"trip_details": None}
    return {"trip_details": serialize_objectid(details)}


@router.put("")
async def save_trip_details(payload: dict = Body(...)):
    db = get_db()
    payload["user_id"] = "Kamal"
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    await db.trip_details.update_one(
        {"user_id": "Kamal"},
        {"$set": payload},
        upsert=True,
    )
    return {"status": "ok"}
