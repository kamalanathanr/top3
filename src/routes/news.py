"""
News routes — aggregated, ranked headlines from public RSS feeds
"""

import asyncio

from datetime import date
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from .. import news_refresh
from ..database import get_db
from ..news_service import (
    NEWS_SOURCES, NEWS_TABS, REFRESH_OPTIONS, RETENTION_OPTIONS, SUGGESTED_TOPICS,
    _as_utc, get_preferences, get_ranked_news, list_saved, save_event_filters, save_preferences, save_sort_mode,
    save_story, unsave_story,
)

router = APIRouter(prefix="/api/news")


@router.get("")
async def list_news():
    return await get_ranked_news(get_db())


@router.get("/status")
async def news_status():
    """Polled by the News page every 30s. If a refresh is due and nothing is
    queued or running, queue one (backup for the worker's scheduler)."""
    status = await asyncio.to_thread(news_refresh.status)
    if status["due"] and not status["refreshing"] and not status["queued"]:
        result = await asyncio.to_thread(news_refresh.queue_refresh, "page", False, "due — requested by open News page")
        status["queued"] = result["queued"]
    return status


@router.post("/refresh")
async def refresh_now():
    """Refresh button: queue a forced refresh in Celery."""
    try:
        return await asyncio.to_thread(news_refresh.queue_refresh, "button", True, "Refresh button")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not queue refresh (is Redis running?): {e}")


@router.post("/events/refresh")
async def refresh_events_now():
    """↻ on an events timeline: queue a forced events refresh in Celery."""
    try:
        return await asyncio.to_thread(news_refresh.queue_refresh, "button", True, "Refresh events button", "events")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not queue refresh (is Redis running?): {e}")


@router.post("/events/pdf")
async def events_pdf(payload: dict = Body(...)):
    """⬇ on a timeline: the events currently shown (already filtered by the page) as an executive PDF."""
    from ..news_pdf import build_events_pdf
    tab = str(payload.get("tab") or "Events")[:40]
    events = payload.get("events") or []
    if not isinstance(events, list) or len(events) > 2000:
        raise HTTPException(status_code=400, detail="events must be a list (max 2000)")
    profile = await get_db().user_profiles.find_one({"user_id": "Kamal"}, {"timezone": 1}) or {}
    pdf = await asyncio.to_thread(build_events_pdf, tab, events, str(payload.get("filters") or "")[:200],
                                  profile.get("timezone") or "Asia/Kolkata")
    filename = f"upcoming-events-{tab.lower().replace(' ', '-')}-{date.today().isoformat()}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/img")
async def article_image(u: str):
    """Relay an article image whose publisher blocks hot-linking. Only URLs stored as an
    article image are served — this is not an open proxy."""
    from ..news_images import relay_image
    db = get_db()
    doc = await db.news_items.find_one({"image": u}, {"link": 1, "article_url": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="unknown image")
    site = doc.get("article_url") or doc.get("link") or ""
    referer = f"{urlparse(site).scheme}://{urlparse(site).netloc}/" if site.startswith("http") else None
    result = await asyncio.to_thread(relay_image, u, referer)
    if not result:
        raise HTTPException(status_code=502, detail="image unavailable")
    ctype, data = result
    return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/refresh-log")
async def refresh_log(limit: int = 100):
    return {"status": await asyncio.to_thread(news_refresh.status),
            "unreachable": await asyncio.to_thread(news_refresh.last_unreachable),
            "entries": await asyncio.to_thread(news_refresh.refresh_log, min(limit, 500))}


@router.get("/preferences")
async def read_preferences():
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"}, {"interests": 1, "hobbies": 1})
    from_profile = []
    for field in ("interests", "hobbies"):
        values = (profile or {}).get(field) or []
        from_profile += [v.strip() for v in values if isinstance(v, str) and v.strip()] if isinstance(values, list) else []
    return {
        "preferences": await get_preferences(db),
        "sources": [{"name": s["name"], "category": s["category"]} for s in NEWS_SOURCES],
        "tabs_catalog": NEWS_TABS,
        "suggested_topics": list(dict.fromkeys(from_profile + SUGGESTED_TOPICS)),
        "retention_options": RETENTION_OPTIONS,
        "refresh_options": REFRESH_OPTIONS,
    }


@router.put("/preferences")
async def update_preferences(payload: dict = Body(...)):
    return {"saved": True, "preferences": await save_preferences(get_db(), payload)}


@router.put("/preferences/sort-mode")
async def update_sort_mode(payload: dict = Body(...)):
    try:
        return {"saved": True, "sort_mode": await save_sort_mode(get_db(), payload.get("sort_mode"))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/preferences/event-filters")
async def update_event_filters(payload: dict = Body(...)):
    try:
        return {"saved": True, "filters": await save_event_filters(get_db(), payload.get("tab"), payload.get("filters") or {})}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/saved")
async def saved_stories():
    return {"items": await list_saved(get_db())}


@router.post("/saved")
async def like_story(payload: dict = Body(...)):
    try:
        return {"saved": True, "item": serialize_saved(await save_story(get_db(), payload.get("link", "")))}
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/saved")
async def unlike_story(link: str):
    await unsave_story(get_db(), link)
    return {"saved": False}


def serialize_saved(doc: dict) -> dict:
    doc = dict(doc)
    for key in ("published_at", "liked_at"):
        if doc.get(key):
            doc["published" if key == "published_at" else key] = _as_utc(doc.pop(key)).isoformat()
    return doc
