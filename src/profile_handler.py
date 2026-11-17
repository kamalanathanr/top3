"""
User profile handling and context generation
"""

from .database import get_db

async def get_user_profile_context() -> str:
    """Get formatted user profile for including in queries"""
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    if not profile:
        return ""

    context_parts = []

    if profile.get("qualification"):
        context_parts.append(f"Qualification: {profile.get('qualification')}")
    if profile.get("family_details"):
        context_parts.append(f"Family Details: {profile.get('family_details')}")
    if profile.get("hobbies"):
        hobbies_str = ", ".join(profile.get("hobbies", []))
        context_parts.append(f"Hobbies: {hobbies_str}")
    if profile.get("history"):
        context_parts.append(f"Background/History: {profile.get('history')}")
    if profile.get("interests"):
        interests_str = ", ".join(profile.get("interests", []))
        context_parts.append(f"General Interests: {interests_str}")
    if profile.get("strategies"):
        context_parts.append(f"Strategies/Preferences: {profile.get('strategies')}")

    if context_parts:
        return "User Profile:\n" + "\n".join(context_parts)
    return ""

async def get_user_timezone() -> str:
    """Get the user's timezone from their profile"""
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    if profile and profile.get("timezone"):
        return profile.get("timezone")
    return "Asia/Kolkata"  # Default timezone
