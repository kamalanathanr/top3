"""
Interest management routes
"""

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse
from datetime import datetime, timezone
from bson.objectid import ObjectId

from ..database import get_db
from ..youtube_search import search_youtube_videos
from ..interest_service import create_queries_for_interest
from ..utils import serialize_objectid

router = APIRouter()

@router.post("/add-interest", response_class=HTMLResponse)
async def add_interest(text: str = Form(...)):
    """Add a new area of interest"""
    text = text.strip()
    if not text:
        return '<div class="text-red-400 text-sm p-3">Error: Empty text</div>'

    db = get_db()

    # Check if already exists (case-insensitive)
    existing = await db.interests.find_one({
        "user_id": "Kamal",
        "text": {"$regex": f"^{text}$", "$options": "i"}
    })
    if existing:
        return '<div class="text-red-400 text-sm p-3">Error: Already exists</div>'

    # Search for videos
    videos = search_youtube_videos(text, max_results=5)

    interest = {
        "user_id": "Kamal",
        "text": text,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "videos": videos
    }

    await db.interests.insert_one(interest)

    # Auto-create YouTube + AI template queries for this interest
    await create_queries_for_interest(text)

    return f"""<div class="p-3 bg-blue-50 rounded-lg hover:bg-blue-100 transition interest-item group border border-blue-200 cursor-pointer" data-text="{text}" onclick="showVideos('{text}')">
        <div class="flex justify-between items-start gap-2">
            <div class="flex-1">
                <p class="text-gray-900 font-medium text-sm">{text}</p>
                <p class="text-gray-600 text-xs mt-1">⏳ Processing...</p>
            </div>
            <button class="opacity-0 group-hover:opacity-100 transition text-red-500 hover:text-red-600 px-2 py-1 rounded hover:bg-red-50 text-sm" onclick="deleteInterest('{text}')">
                ✕
            </button>
        </div>
    </div>"""

@router.get("/interests")
async def get_interests():
    """Get all active interests for current user"""
    db = get_db()
    interests = await db.interests.find({"user_id": "Kamal", "status": "active"}).to_list(None)
    # Normalize text field for consistent case
    for interest in interests:
        if "text" in interest and interest["text"]:
            interest["text"] = interest["text"].strip()
    interests = [serialize_objectid(i) for i in interests]
    return {"interests": interests}

@router.post("/delete-interest/{interest_text}", response_class=HTMLResponse)
async def delete_interest(interest_text: str):
    """Mark an interest as deleted (soft delete)"""
    db = get_db()
    # Soft delete: mark as deleted instead of removing (case-insensitive)
    await db.interests.update_one(
        {"user_id": "Kamal", "text": {"$regex": f"^{interest_text}$", "$options": "i"}},
        {"$set": {"status": "deleted", "deleted_at": datetime.now(timezone.utc).isoformat()}}
    )
    # Also mark all related queries as deleted (case-insensitive)
    await db.queries.update_many(
        {"user_id": "Kamal", "user_interests": {"$regex": f"^{interest_text}$", "$options": "i"}},
        {"$set": {"status": "cancelled"}}
    )

    interests = await db.interests.find({"user_id": "Kamal", "status": "active"}).to_list(None)

    if not interests:
        return '<div id="empty-state" class="text-gray-500 text-sm text-center py-6">No interests yet. Add one above to get started.</div>'

    return ''.join([
        f"""<div class="p-3 bg-blue-50 rounded-lg hover:bg-blue-100 transition interest-item group border border-blue-200 cursor-pointer" data-text="{i['text']}" onclick="showVideos('{i['text']}')">
            <div class="flex justify-between items-start gap-2">
                <div class="flex-1">
                    <p class="text-gray-900 font-medium text-sm">{i['text']}</p>
                    <p class="text-gray-600 text-xs mt-1">⏳ Processing...</p>
                </div>
                <button class="opacity-0 group-hover:opacity-100 transition text-red-500 hover:text-red-600 px-2 py-1 rounded hover:bg-red-50 text-sm" onclick="deleteInterest('{i['text']}')">
                    ✕
                </button>
            </div>
        </div>"""
        for i in interests
    ])
