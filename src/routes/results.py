"""
Query results routes
"""

from fastapi import APIRouter
from bson.objectid import ObjectId
from datetime import datetime, timezone, timedelta

from ..database import get_db
from ..models import ResultUpdate
from ..utils import serialize_objectid

router = APIRouter(prefix="/api/results")

@router.get("")
async def list_results():
    """List all queries with their results (left join — includes in-progress and failed)"""
    db = get_db()

    # Mark any query stuck in "running" for > 10 minutes as "error"
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    await db.queries.update_many(
        {"status": "running", "started_at": {"$lt": stale_cutoff}},
        {"$set": {"status": "error", "error": "Task timed out (no result after 10 minutes)"}}
    )

    # Start from queries, left-join to query_results
    pipeline = [
        {"$match": {"user_id": "Kamal"}},
        {"$sort": {"created_at": -1}},
        {
            "$lookup": {
                "from": "query_results",
                "localField": "_id",
                "foreignField": "query_id",
                "as": "result"
            }
        },
        {
            "$project": {
                "_id": 1,
                "query_type": 1,
                "template_name": 1,
                "user_interests": 1,
                "interests": 1,
                "ai_query": 1,
                "status": 1,
                "error": 1,
                "created_at": 1,
                "started_at": 1,
                "completed_at": 1,
                "result": {"$arrayElemAt": ["$result", 0]}
            }
        }
    ]

    queries = await db.queries.aggregate(pipeline).to_list(None)
    queries = [serialize_objectid(q) for q in queries]
    return {"results": queries}


@router.put("/{result_id}")
async def update_result(result_id: str, request: ResultUpdate):
    """Update a query result"""
    db = get_db()
    result = await db.query_results.update_one(
        {"_id": ObjectId(result_id)},
        {"$set": {"output": request.output}}
    )
    return {"updated": result.modified_count > 0}
