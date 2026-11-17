"""
Timezone utilities for consistent timestamp handling across the application
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .database import get_db

async def get_user_timezone() -> str:
    """Get the user's timezone from their profile"""
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    if profile and profile.get("timezone"):
        return profile.get("timezone")
    return "Asia/Kolkata"  # Default timezone

def convert_utc_to_user_timezone(utc_iso_string: str, user_timezone: str) -> str:
    """Convert UTC ISO string to user's timezone.
    Treats naive datetimes (no tzinfo) as UTC for backward compatibility with old records.
    """
    try:
        utc_dt = datetime.fromisoformat(utc_iso_string.replace('Z', '+00:00'))
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        user_tz = ZoneInfo(user_timezone)
        local_dt = utc_dt.astimezone(user_tz)
        return local_dt.strftime("%d %b %Y %H:%M:%S %Z")
    except Exception as e:
        print(f"Error converting timezone: {e}")
        return utc_iso_string

def get_current_utc_timestamp() -> str:
    """Get current UTC timestamp as timezone-aware ISO string"""
    return datetime.now(timezone.utc).isoformat()
