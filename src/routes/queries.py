"""
Query management routes
"""

from fastapi import APIRouter
from datetime import datetime, timezone
from bson.objectid import ObjectId
import redis

from ..database import get_db
from ..models import QueryRequest
from ..utils import serialize_objectid
from ..tasks import execute_query

router = APIRouter(prefix="/api/queries")
redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

@router.post("")
async def create_query(request: QueryRequest):
    """Create a new query and enqueue to Celery"""
    db = get_db()

    # Get template
    template_doc = await db.query_templates.find_one({"_id": ObjectId(request.template_id)})
    if not template_doc:
        return {"error": "Template not found"}

    # Build full query
    interests_str = ", ".join(request.user_interests)
    ai_query = template_doc["template"].format(interests=interests_str, profile="solo traveler")

    # Create query document
    query_doc = {
        "user_id": "Kamal",
        "template_id": ObjectId(request.template_id),
        "template_name": template_doc["name"],
        "user_interests": request.user_interests,
        "ai_query": ai_query,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    result = await db.queries.insert_one(query_doc)
    query_id = str(result.inserted_id)

    # Enqueue to Celery
    execute_query.delay(query_id)

    return {"query_id": query_id, "status": "pending"}

@router.get("")
async def list_queries():
    """List all queries for current user"""
    db = get_db()
    queries = await db.queries.find({"user_id": "Kamal"}).sort("created_at", -1).to_list(None)
    queries = [serialize_objectid(q) for q in queries]
    return {"queries": queries}

@router.post("/{query_id}/pause")
async def pause_query(query_id: str):
    """Pause a query"""
    db = get_db()
    result = await db.queries.update_one(
        {"_id": ObjectId(query_id)},
        {"$set": {"status": "paused"}}
    )
    return {"paused": result.modified_count > 0}

@router.delete("/{query_id}")
async def cancel_query(query_id: str):
    """Cancel a query"""
    db = get_db()
    result = await db.queries.update_one(
        {"_id": ObjectId(query_id)},
        {"$set": {"status": "cancelled"}}
    )
    return {"cancelled": result.modified_count > 0}
