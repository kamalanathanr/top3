"""
Video streaming and search routes
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Query, BackgroundTasks
from bson.objectid import ObjectId
from datetime import datetime, timezone

from ..youtube_search import get_video_stream, search_youtube_videos
from ..database import get_db

router = APIRouter()
executor = ThreadPoolExecutor(max_workers=3)

@router.get("/video/{video_id}")
async def get_video(video_id: str):
    """Get video stream URL from YouTube"""
    result = await get_video_stream(video_id)
    return result

@router.post("/api/search-youtube/{item_id}")
async def search_youtube_for_item(item_id: str, title: str = Query(...), background_tasks: BackgroundTasks = None):
    """Search YouTube videos for a specific item/tab and store results"""
    try:
        # Run YouTube search in background thread (non-blocking)
        loop = asyncio.get_event_loop()
        videos = await asyncio.wait_for(
            loop.run_in_executor(executor, lambda: search_youtube_videos(title, max_results=5)),
            timeout=30.0
        )

        # Schedule storing results in background
        if background_tasks:
            background_tasks.add_task(store_youtube_results, item_id, videos)

        return {
            "success": True,
            "videos": videos,
            "message": "Videos found successfully"
        }
    except asyncio.TimeoutError:
        print(f"YouTube search timeout for: {title}")
        return {
            "success": False,
            "error": "YouTube search timed out",
            "videos": []
        }
    except Exception as e:
        print(f"YouTube search error for '{title}': {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "videos": []
        }

async def store_youtube_results(item_id: str, videos: list):
    """Store YouTube results in MongoDB in background"""
    try:
        db = get_db()
        await db.query_results.update_one(
            {"_id": ObjectId(item_id)},
            {
                "$set": {
                    "youtube_videos": videos,
                    "youtube_search_completed_at": datetime.now(timezone.utc).isoformat()
                }
            }
        )
        print(f"Stored {len(videos)} YouTube videos for item {item_id}")
    except Exception as e:
        print(f"Error storing YouTube results: {str(e)}")
